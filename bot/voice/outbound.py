"""Исходящий звонок владельцу: place_call принимается только от Кимо. Кимо звонит только по заданиям call_owner.

Не ответил за 30 секунд — отбой, результат failed с причиной; Кимо пришлёт её сообщением в Telegram. Если звонок даже не
начал звонить, вероятно, Android усыпил Linphone: это не ошибка кода, причина пишется в лог."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bot.agents.bus import BusError, BusWorker
from bot.voice.service import VoiceService
from bot.voice.session import CallSession, VoiceDeps
from bot.voice.stack import CallHandler, SipStack

log = logging.getLogger("voice.out")

CALLER = "kimo"  # единственный, кто может заказать звонок


class ProxyHandler:
    """Обработчик, который привязывается к сессии позже: звонок создаётся раньше, чем сессия."""

    def __init__(self) -> None:
        self._target: CallHandler | None = None
        self._pending: list[tuple[str, tuple[Any, ...]]] = []

    def bind(self, target: CallHandler) -> None:
        self._target = target
        pending, self._pending = self._pending, []
        for name, args in pending:
            getattr(target, name)(*args)

    def _forward(self, name: str, *args: Any) -> None:
        if self._target is None:
            self._pending.append((name, args))
        else:
            getattr(self._target, name)(*args)

    def on_early(self) -> None:
        self._forward("on_early")

    def on_connected(self) -> None:
        self._forward("on_connected")

    def on_audio(self, frame: bytes) -> None:
        self._forward("on_audio", frame)

    def on_ended(self, reason: str) -> None:
        self._forward("on_ended", reason)


def describe_failure(early_seen: bool, end_reason: str, timed_out: bool, ring_seconds: int) -> str:
    """Понятная причина, почему звонок владельцу не состоялся."""
    if timed_out and not early_seen:
        return ("Linphone не отвечает: звонок даже не начал звонить (возможно, Android усыпил приложение "
                "или адрес недоступен)")
    if timed_out:
        return f"владелец не ответил за {ring_seconds} с"
    code = end_reason.split(" ", 1)[0]
    if code in ("603", "486"):
        return "владелец отклонил звонок"
    if code in ("408", "480", "503", "404"):
        return f"звонок не дошёл ({end_reason}); возможно, Android усыпил Linphone"
    return f"звонок завершился до ответа ({end_reason or 'без причины'})"


class OutboundCaller:
    def __init__(self, stack: SipStack, service: VoiceService, deps: VoiceDeps) -> None:
        self.stack, self.service, self.deps = stack, service, deps

    async def place(self, task: dict[str, Any]) -> str:
        cfg = self.deps.cfg
        if task["from_agent"] != CALLER:
            raise BusError("Звонить владельцу можно только по заданию Кимо")
        payload = task["payload"]
        if payload.get("type") != "place_call":
            raise BusError(f"Неизвестный тип задания: {payload.get('type')}")
        if not cfg.callback_uri:
            raise BusError("Не задан SIP_CALLBACK_URI в .env: некуда звонить")
        if self.service.busy():
            raise BusError("линия занята: идёт разговор")
        reason = str(payload.get("reason", "")).strip()[:200]
        proxy = ProxyHandler()
        call = self.stack.call_out(cfg.callback_uri, proxy)
        opening = f"Кимо на связи. {reason}".strip()
        session = CallSession(call, self.service, self.deps, opening=opening)
        proxy.bind(session)
        log.info("Исходящий звонок владельцу: срочно=%s", bool(payload.get("urgent")))
        connected = asyncio.ensure_future(session.connected.wait())
        ended = asyncio.ensure_future(session.ended.wait())
        try:
            await asyncio.wait({connected, ended}, timeout=cfg.outbound_ring_seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (connected, ended):
                fut.cancel()
        if session.connected.is_set():
            return "владелец ответил, разговор идёт"
        timed_out = not session.ended.is_set()
        if timed_out:
            call.hangup()  # не ответил за отведённое время: отбой
            await asyncio.sleep(0)
        why = describe_failure(session.early.is_set(), session.end_reason, timed_out, cfg.outbound_ring_seconds)
        log.warning("Звонок владельцу не состоялся: %s", why)
        if timed_out:  # сессия ещё не завершена стеком: закрываем сами, чтобы линия освободилась
            session.on_ended(why)
        raise BusError(why)


class NoBot:
    """Заглушка Telegram для воркера голосовой службы: сообщения владельцу она отправляет только через Кимо."""

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        log.info("Сообщение в Telegram из службы звонков игнорируется (отправляет Кимо)")


class ServiceWorker(BusWorker):
    """Воркер службы: берёт задания place_call. Результаты своих заданий не доставляет: ими занимается BusKimoLink."""

    async def deliver_results(self) -> int:
        return 0
