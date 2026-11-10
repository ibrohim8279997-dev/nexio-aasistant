"""Watchdog в процессе Кимо: быстрый таймаут заданий и понятная причина вместо молчания.

Правила (раз в WATCH_INTERVAL секунд):
- задание new: агент offline дольше OFFLINE_GRACE или задание не взято за timeout -> failed + сообщение владельцу;
- задание taken: агент упал (offline) или задание висит дольше taken_timeout -> failed + сообщение владельцу.
Таймауты настраиваются в конфиге агента: task_timeout_s (по умолчанию 60), taken_timeout_s (по умолчанию 3 x task_timeout_s),
например для агентов с веб-поиском.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from bot.agents.health import (
    OFFLINE_AFTER,
    effective_status,
    reason_text,
    seconds_since,
)
from bot.agents.registry import AgentConfig, Registry
from bot.db import Database
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

WATCH_INTERVAL = 10.0
DEFAULT_TIMEOUT = 60.0
OFFLINE_GRACE = 10.0  # offline-агент: не ждём весь таймаут, но даём 10 с на запуск
INTERNAL_TYPES = {"ping", "build_card"}  # служебные задания: о провале узнаёт тот, кто их ждёт

Notifier = Callable[[str, AgentConfig | None], Awaitable[None]]


def task_timeout(agent: AgentConfig | None) -> float:
    try:
        return float((agent.config if agent else {}).get("task_timeout_s", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def taken_timeout(agent: AgentConfig | None) -> float:
    try:
        return float((agent.config if agent else {}).get("taken_timeout_s", task_timeout(agent) * 3))
    except (TypeError, ValueError):
        return task_timeout(agent) * 3


class Watchdog:
    def __init__(self, db: Database, registry: Registry, notify: Notifier, own_agent: str = "kimo",
                 interval: float = WATCH_INTERVAL) -> None:
        self._db = db
        self._registry = registry
        self._notify = notify
        self.own_agent = own_agent
        self.interval = interval
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        log.info("Watchdog заданий запущен (каждые %.0f с)", self.interval)
        while not self._stopped.is_set():
            try:
                await self.check()
                await self._db.pending_expire(to_db(now_utc()))
            except Exception:  # noqa: BLE001
                log.exception("Watchdog: ошибка проверки")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def check(self) -> list[int]:
        """Один проход. Возвращает номера заданий, закрытых как failed."""
        now = now_utc()
        agents = {a.name: a for a in await self._registry.all()}
        failed: list[int] = []
        for task in await self._db.queue_open():
            target = agents.get(task["to_agent"])
            if task["to_agent"] == self.own_agent:
                continue  # задания самому Кимо принимает его собственный воркер
            reason = self._stuck_reason(task, target, now)
            if reason is None:
                continue
            display = target.display_name if target else task["to_agent"]
            result = f"⚠️ {reason}"
            if not await self._db.queue_force_fail(task["id"], result, to_db(now)):
                continue  # агент успел завершить сам
            failed.append(task["id"])
            log.warning("Watchdog: задание #%s (%s) закрыто: %s", task["id"], display, reason)
            payload = task.get("payload") or {}
            if payload.get("deliver_by") != "inline":
                await self._db.queue_mark_delivered(task["id"])  # владельца уведомляем здесь, отправитель не дублирует
            if payload.get("type") in INTERNAL_TYPES:
                continue
            origin = agents.get(task["from_agent"])
            who = origin.display_name if origin else task["from_agent"]
            about = str((payload.get("context") or {}).get("about") or payload.get("text") or "")[:120]
            text = f"⚠️ Задание #{task['id']} от {who} к {display} не выполнено.\n{reason}"
            if about:
                text += f"\nЗадание: «{about}»"
            await self._notify(text, target)
        return failed

    def _stuck_reason(self, task: dict[str, Any], target: AgentConfig | None, now: Any) -> str | None:
        display = target.display_name if target else task["to_agent"]
        if target is None:
            return f"Агент «{task['to_agent']}» не найден в реестре"
        status = effective_status(target.last_seen, target.status, now)
        detail = target.status_detail
        if task["status"] == "new":
            age = seconds_since(task["created_at"], now) or 0.0
            timeout = task_timeout(target)
            if status == "offline" and (age >= OFFLINE_GRACE or not target.enabled):
                return reason_text(display, target.last_seen, target.status, target.last_error, detail, now)
            if age >= timeout:
                if status in ("busy", "error"):
                    return reason_text(display, target.last_seen, target.status, target.last_error, detail, now)
                return f"{display} не взял задание за {int(timeout)} с (процесс жив, но очередь не читает)"
            return None
        # taken: считаем от момента, когда агент взял задание
        taken_age = seconds_since(task["updated_at"], now) or 0.0
        if status == "offline" and taken_age >= OFFLINE_GRACE and (seconds_since(target.last_seen, now) or 1e9) > OFFLINE_AFTER:
            return f"{display} перестал отвечать во время выполнения (последний heartbeat давно): " + reason_text(
                display, target.last_seen, target.status, target.last_error, detail, now
            )
        if taken_age >= taken_timeout(target):
            return f"{display} выполняет задание дольше {int(taken_timeout(target))} с — считаю его зависшим"
        return None


def format_offline(agent: AgentConfig, now: Any = None) -> str:
    """Сообщение «агент offline» с причиной и предложением перезапуска (для передачи задач)."""
    return escape(reason_text(agent.display_name, agent.last_seen, agent.status, agent.last_error, agent.status_detail, now))
