"""Живость агентов: heartbeat в таблице agents, состояние процесса, вывод статуса offline по last_seen."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot.db import Database
from bot.logging_setup import redact_text
from bot.timeutil import TZ, from_db, now_utc, to_db

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 20.0  # раз в 15-30 с
OFFLINE_AFTER = 90.0  # нет свежего last_seen дольше — агент offline
ERROR_WINDOW = 120.0  # столько секунд после ошибки статус error

STATUS_ICON = {"online": "🟢", "busy": "🟡", "error": "🔴", "offline": "⚫"}
STATUS_RU = {"online": "online", "busy": "busy", "error": "error", "offline": "offline"}


def seconds_since(last_seen: str | None, now: datetime | None = None) -> float | None:
    seen = from_db(last_seen)
    if seen is None:
        return None
    return ((now or now_utc()) - seen).total_seconds()


def effective_status(last_seen: str | None, status: str | None, now: datetime | None = None) -> str:
    """online / busy / error, а если heartbeat старше 90 с или его не было — offline."""
    age = seconds_since(last_seen, now)
    if age is None or age > OFFLINE_AFTER or status == "offline":
        return "offline"
    return status if status in ("online", "busy", "error") else "online"


def fmt_ago(seconds: float | None) -> str:
    if seconds is None:
        return "никогда"
    seconds = max(int(seconds), 0)
    if seconds < 90:
        return f"{seconds} с назад"
    if seconds < 5400:
        return f"{seconds // 60} мин назад"
    if seconds < 172800:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def fmt_clock(iso_utc: str | None) -> str:
    dt = from_db(iso_utc)
    return dt.astimezone(TZ).strftime("%H:%M:%S") if dt else "—"


def reason_text(display: str, last_seen: str | None, status: str | None, last_error: str | None,
                detail: dict[str, Any] | None, now: datetime | None = None) -> str:
    """Одна строка: почему агент не отвечает (offline, лимит Groq, ошибка)."""
    eff = effective_status(last_seen, status, now)
    detail = detail or {}
    if eff == "offline":
        age = fmt_ago(seconds_since(last_seen, now))
        base = f"{display} offline: процесс не отвечает (последний heartbeat: {age})"
        return base + (f"; последняя ошибка: {last_error}" if last_error else "")
    if eff == "busy" and detail.get("reason") == "rate_limit":
        return f"{display}: лимит Groq (429), повтор запроса около {detail.get('until_clock', '—')}"
    if eff == "busy":
        return f"{display} занят другим заданием"
    if eff == "error":
        return f"{display}: ошибка — {last_error or 'причина не записана'}"
    return f"{display} online"


@dataclass
class AgentState:
    """Живое состояние процесса агента (читает Heartbeat, пишут воркер и клиент Groq)."""

    busy_tasks: int = 0
    last_error: str | None = None
    last_error_at: float = 0.0
    started_at: float = 0.0

    def set_error(self, text: str) -> None:
        self.last_error = redact_text(text)[:300]  # без токенов и ключей
        self.last_error_at = time.time()

    def snapshot(self, rate_limit_until: float = 0.0, now_epoch: float | None = None) -> tuple[str, str | None, dict[str, Any]]:
        now_epoch = time.time() if now_epoch is None else now_epoch
        if rate_limit_until > now_epoch:
            until = datetime.fromtimestamp(rate_limit_until, tz=timezone.utc).astimezone(TZ)
            return "busy", self.last_error, {
                "reason": "rate_limit", "until": to_db(datetime.fromtimestamp(rate_limit_until, tz=timezone.utc)),
                "until_clock": until.strftime("%H:%M:%S"),
            }
        if self.busy_tasks > 0:
            return "busy", self.last_error, {"reason": "task", "tasks": self.busy_tasks}
        if self.last_error and now_epoch - self.last_error_at < ERROR_WINDOW:
            return "error", self.last_error, {"reason": "error"}
        return "online", self.last_error, {}


class Heartbeat:
    """Каждые 15-30 с пишет в agents: last_seen, status (online/busy/error), последнюю ошибку без секретов."""

    def __init__(self, db: Database, agent: str, state: AgentState, llm: Any = None,
                 interval: float = HEARTBEAT_INTERVAL) -> None:
        self._db = db
        self.agent = agent
        self.state = state
        self.llm = llm
        self.interval = interval
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def beat(self) -> None:
        limit = float(getattr(self.llm, "rate_limit_until", 0.0) or 0.0)
        status, error, detail = self.state.snapshot(limit)
        await self._db.agent_heartbeat(self.agent, status, to_db(now_utc()), error, detail)

    async def run(self) -> None:
        log.info("Heartbeat агента %s: каждые %.0f с", self.agent, self.interval)
        while not self._stopped.is_set():
            try:
                await self.beat()
            except Exception:  # noqa: BLE001 — БД занята: пробуем в следующий раз
                log.warning("Heartbeat: не удалось записать")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
        try:  # штатная остановка: сразу offline, не ждём 90 секунд
            await self._db.agent_heartbeat(self.agent, "offline", to_db(now_utc()), None, {"reason": "stopped"})
        except Exception:  # noqa: BLE001
            pass
