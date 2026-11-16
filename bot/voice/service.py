"""Служба звонков: состояние (один звонок за раз), журнал каждого звонка и запись в audit_log."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bot.agents.health import AgentState
from bot.db import Database
from bot.logging_setup import redact_text
from bot.timeutil import now_utc, to_db
from bot.voice.config import VoiceConfig
from bot.voice.stack import CallHandler, SipCall

log = logging.getLogger("voice.calls")


@dataclass
class CallRecord:
    call_id: str
    direction: str  # in | out
    remote: str
    started: float = field(default_factory=time.monotonic)
    started_wall: datetime = field(default_factory=lambda: datetime.now())  # noqa: DTZ005 — для строки в логе
    connected: float | None = None
    phrases: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


SessionFactory = Callable[[SipCall, "VoiceService"], CallHandler]


class VoiceService:
    def __init__(self, cfg: VoiceConfig, db: Database, state: AgentState, session_factory: SessionFactory) -> None:
        self.cfg = cfg
        self.db = db
        self.state = state
        self._factory = session_factory
        self.active: CallRecord | None = None

    def busy(self) -> bool:
        return self.active is not None

    # ---------- жизненный цикл звонка ----------

    def begin(self, call: SipCall) -> CallRecord | None:
        """Занять линию. None — линия уже занята (параллельные звонки не поддерживаются)."""
        if self.active is not None:
            return None
        self.active = CallRecord(call.call_id, call.direction, call.remote)
        self.state.busy_tasks += 1
        log.info("Звонок начался: id=%s направление=%s адрес=%s", call.call_id, call.direction, call.remote)
        return self.active

    def on_incoming(self, call: SipCall) -> CallHandler:
        """Вызывается стеком после проверки пароля и ответа 200."""
        return self._factory(call, self)

    def finish(self, record: CallRecord, reason: str) -> None:
        """Журнал звонка: время, длительность, причина завершения. Звук не сохраняется."""
        if self.active is not record:
            return
        self.active = None
        self.state.busy_tasks = max(self.state.busy_tasks - 1, 0)
        duration = round(time.monotonic() - (record.connected or record.started))
        reason = redact_text(reason)
        log.info(
            "Звонок завершён: id=%s направление=%s адрес=%s начало=%s длительность=%dс причина=%s фраз=%d",
            record.call_id, record.direction, record.remote, record.started_wall.strftime("%Y-%m-%d %H:%M:%S"),
            duration, reason, record.phrases,
        )
        details = {"call": record.call_id, "direction": record.direction, "remote": record.remote,
                   "duration_s": duration, "end_reason": reason, "phrases": record.phrases, **record.extra}
        try:
            asyncio.get_running_loop().create_task(self._audit("voice_call", details))
        except RuntimeError:
            pass

    async def _audit(self, action: str, details: dict[str, Any]) -> None:
        try:
            await self.db.audit(to_db(now_utc()), "voice_sip", action, None, details)
        except Exception:  # noqa: BLE001 — сбой аудита не должен рвать звонок
            log.exception("audit_log: не удалось записать %s", action)

    async def audit(self, action: str, target: str | None, details: dict[str, Any]) -> None:
        try:
            await self.db.audit(to_db(now_utc()), "voice_sip", action, target, details)
        except Exception:  # noqa: BLE001
            log.exception("audit_log: не удалось записать %s", action)


class LoggingSession:
    """Сессия звонка этапа 1: принимает звонок и ведёт журнал. Распознавание речи и озвучка подключаются в session.py."""

    def __init__(self, call: SipCall, service: VoiceService) -> None:
        self.call, self.service = call, service
        self.record = service.begin(call)
        if self.record is None:
            call.hangup()

    def on_early(self) -> None:
        pass

    def on_connected(self) -> None:
        if self.record:
            self.record.connected = time.monotonic()

    def on_audio(self, frame: bytes) -> None:
        pass

    def on_ended(self, reason: str) -> None:
        if self.record:
            self.service.finish(self.record, reason)
