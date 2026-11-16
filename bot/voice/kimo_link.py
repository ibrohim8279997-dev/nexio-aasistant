"""Связь голосовой службы с Кимо через очередь заданий (tasks_queue): фраза -> voice_command, текст в чат -> tg_message.

Правила доступа не меняются: голос — ещё один способ ввода от владельца, решения принимает Кимо. Задание всегда
содержит chat_id владельца, поэтому ответ не теряется. Каждое обращение пишется в audit_log."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from bot.agents.bus import Bus, BusError
from bot.agents.health import effective_status
from bot.db import Database
from bot.logging_setup import redact_text
from bot.voice.service import VoiceService
from bot.voice.session import KimoReply, Relay

log = logging.getLogger("voice.kimo")

LATE_SECONDS = 300.0  # столько ждём запоздавший ответ, чтобы прислать его в Telegram


def parse_result(result: str | None) -> tuple[str, list[str]]:
    """Результат voice_command: JSON {text, answered_by}; на всякий случай принимаем и обычный текст."""
    raw = (result or "").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "text" in data:
            return str(data["text"]), [str(a) for a in data.get("answered_by", ["kimo"])]
    except ValueError:
        pass
    return raw, ["kimo"]


class BusRelay:
    """Тексты в Telegram идут через Кимо: токен бота в процесс звонка не попадает."""

    def __init__(self, bus: Bus, owner_id: int) -> None:
        self._bus, self._owner = bus, owner_id

    async def send(self, text: str) -> None:
        await self._bus.send("kimo", {"type": "tg_message", "text": text[:12000]}, depth=1, chat_id=self._owner)


class BusKimoLink:
    def __init__(self, bus: Bus, db: Database, relay: Relay, service: VoiceService, wait_seconds: float,
                 owner_id: int, late_seconds: float = LATE_SECONDS) -> None:
        self._bus, self._db, self._relay, self._service = bus, db, relay, service
        self._wait, self._owner, self._late = wait_seconds, owner_id, late_seconds
        self._late_tasks: set[asyncio.Task[None]] = set()

    async def ask(self, text: str, history: list[tuple[str, str]], call_id: str) -> KimoReply:
        row = await self._db.get_agent("kimo")
        if row is None or effective_status(row["last_seen"], row["status"]) == "offline":
            log.error("Кимо offline: фраза не передана")
            await self._audit(text, ["kimo"], False, call_id, "Кимо offline")
            return KimoReply("Кимо сейчас недоступен.", failed=True)
        payload: dict[str, Any] = {
            "type": "voice_command", "source": "voice_call", "text": text,
            "context": {
                "from": "voice_sip", "from_name": "Голос", "owner_request": text[:500], "about": text[:300],
                "need": "выполнить голосовую команду владельца", "call": call_id,
                "history": [{"user": u, "kimo": k} for u, k in history[-3:]],
            },
        }
        task_id = await self._bus.send("kimo", payload, depth=1, chat_id=self._owner)
        try:
            task = await self._bus.wait_result(task_id, timeout=self._wait, interval=0.3)
        except BusError:
            log.warning("Кимо не ответил за %.0f с (задание #%s)", self._wait, task_id)
            self._spawn(self._deliver_late(task_id, text))
            await self._audit(text, ["kimo"], False, call_id, "тайм-аут")
            return KimoReply("", timed_out=True, task_id=task_id)
        await self._db.queue_mark_delivered(task_id)
        if task["status"] == "failed":
            reason = (task["result"] or "ошибка").removeprefix("⚠️").strip()
            await self._audit(text, ["kimo"], False, call_id, reason)
            return KimoReply(reason, failed=True, task_id=task_id)
        answer, who = parse_result(task["result"])
        await self._audit(text, who, True, call_id)
        return KimoReply(answer, answered_by=who[-1], task_id=task_id)

    async def _audit(self, phrase: str, answered_by: list[str], ok: bool, call_id: str, note: str = "") -> None:
        """audit_log: время, текст фразы, какой агент ответил."""
        await self._service.audit(
            "voice_phrase", ",".join(answered_by),
            {"text": redact_text(phrase)[:500], "answered_by": answered_by, "ok": ok, "call": call_id, "note": note},
        )

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._late_tasks.add(task)
        task.add_done_callback(self._late_tasks.discard)

    async def _deliver_late(self, task_id: int, phrase: str) -> None:
        """Ответ пришёл после тайм-аута: присылаем его в Telegram, а не теряем."""
        try:
            task = await self._bus.wait_result(task_id, timeout=self._late, interval=1.0)
        except BusError:
            log.error("Задание #%s так и не выполнено за %.0f с", task_id, self._late)
            return
        await self._db.queue_mark_delivered(task_id)
        text = parse_result(task["result"])[0] if task["status"] == "done" else f"не получилось: {task['result']}"
        await self._relay.send(f"📞 Ответ на «{phrase[:80]}» пришёл позже:\n{text}")
