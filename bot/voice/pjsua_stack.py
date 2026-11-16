"""Адаптер pjsua2: UDP SIP только на адресе Tailscale и localhost, нулевое звуковое устройство, аккаунт без регистрации.

Авторизацию входящих делает IncomingGate (digest.py): pjsua2 сам проверять входящие пароли не умеет, поэтому
первый INVITE получает 401 с вызовом, повторный с Authorization проверяется нами. Звук идёт через AudioMediaPort.

Модуль pjsua2 импортируется только в start(), поэтому остальной код и тесты работают без него.
Потоки pjsip вызывают колбэки в своих потоках, события переносятся в asyncio через call_soon_threadsafe."""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from bot.voice.config import VoiceConfig
from bot.voice.digest import IncomingGate, extract_authorization
from bot.voice.stack import FRAME_MS, RATE, CallHandler, SipCall, split_frames

log = logging.getLogger("voice.sip")

CODEC_PRIORITY = (("opus/", 255), ("PCMU/", 254), ("PCMA/", 253))  # остальные кодеки выключены


def _host(src_address: str) -> str:
    """'100.64.0.5:5060' или '[::1]:5060' -> адрес без порта."""
    host = src_address.rsplit(":", 1)[0]
    return host.strip("[]")


class _Handle:
    """SipCall поверх pjsua2: очередь исходящих кадров и доставка событий обработчику в потоке asyncio."""

    def __init__(self, stack: Pjsua2Stack, direction: str, remote: str) -> None:
        self._stack = stack
        self.direction = direction
        self.remote = remote
        self.call_id = ""
        self.pj_call: Any = None
        self.port: Any = None
        self._out: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._handler: CallHandler | None = None
        self._pending: list[tuple[str, tuple[Any, ...]]] = []
        self.ended = False

    # --- SipCall ---
    def send_audio(self, pcm: bytes) -> None:
        with self._lock:
            self._out.extend(split_frames(pcm))

    def clear_audio(self) -> None:
        with self._lock:
            self._out.clear()

    def pending_ms(self) -> int:
        with self._lock:
            return len(self._out) * FRAME_MS

    def hangup(self) -> None:
        call, pj = self.pj_call, self._stack.pj
        if call is None or self.ended:
            return
        try:
            call.hangup(pj.CallOpParam(True))
        except Exception as exc:  # noqa: BLE001 — звонок мог уже завершиться
            log.info("hangup: %s", type(exc).__name__)

    # --- вызывается потоком звука pjsip ---
    def pop_frame(self) -> bytes | None:
        with self._lock:
            return self._out.popleft() if self._out else None

    # --- события: из потока pjsip в asyncio ---
    def bind(self, handler: CallHandler) -> None:
        self._handler = handler
        pending, self._pending = self._pending, []
        for name, args in pending:
            getattr(handler, name)(*args)

    def emit(self, name: str, *args: Any) -> None:
        self._stack.loop.call_soon_threadsafe(self._dispatch, name, args)

    def _dispatch(self, name: str, args: tuple[Any, ...]) -> None:
        if self._handler is None:
            self._pending.append((name, args))
        else:
            getattr(self._handler, name)(*args)

    def attach_media(self, audio_media: Any) -> None:
        pj = self._stack.pj
        if self.port is None:
            self.port = self._stack.make_port(self)
        audio_media.startTransmit(self.port)  # звонок -> наш порт (onFrameReceived)
        self.port.startTransmit(audio_media)  # наш порт -> звонок (onFrameRequested)
        log.debug("медиа звонка %s подключено (%s)", self.call_id, pj.__name__)


class Pjsua2Stack:
    def __init__(self, cfg: VoiceConfig, gate: IncomingGate, on_incoming: Callable[[SipCall], CallHandler],
                 loop: asyncio.AbstractEventLoop) -> None:
        self.cfg = cfg
        self.gate = gate
        self._on_incoming = on_incoming
        self.loop = loop
        self.pj: Any = None
        self.ep: Any = None
        self.account: Any = None
        self._calls: dict[int, _Handle] = {}
        self._keep: dict[int, Any] = {}  # объекты Call нельзя удалять, пока звонок жив
        self._bytevec_signed = True

    # ---------- запуск ----------

    def start(self) -> None:
        import pjsua2 as pj  # noqa: PLC0415 — только на сервере, где собран PJSIP

        self.pj = pj
        stack = self
        ep = pj.Endpoint()
        ep.libCreate()
        ep_cfg = pj.EpConfig()
        ep_cfg.logConfig.level = 3  # SIP-сообщения (с заголовком Authorization) в лог не попадают
        ep_cfg.logConfig.consoleLevel = 1
        ep_cfg.uaConfig.userAgent = "Kimo-Voice"
        ep.libInit(ep_cfg)
        for address in dict.fromkeys([self.cfg.bind_address, "127.0.0.1"]):  # только Tailscale и localhost
            tcfg = pj.TransportConfig()
            tcfg.port = self.cfg.sip_port
            tcfg.boundAddress = address
            ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
            log.info("SIP слушает UDP %s:%d", address, self.cfg.sip_port)
        ep.libStart()
        ep.audDevManager().setNullDev()  # звуковой карты нет: звук обрабатывается программно
        self.ep = ep
        self._configure_codecs()

        class Port(pj.AudioMediaPort):
            def __init__(self, handle: _Handle) -> None:
                super().__init__()
                self._h = handle

            def onFrameRequested(self, frame):  # noqa: N802 — имя задано pjsua2
                data = self._h.pop_frame()
                if data is None:
                    frame.type = pj.PJMEDIA_FRAME_TYPE_NONE
                    frame.size = 0
                    return
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = stack._to_bytevec(data)
                frame.size = len(data)

            def onFrameReceived(self, frame):  # noqa: N802
                if frame.size:
                    data = bytes(b & 0xFF for b in list(frame.buf)[: frame.size])
                    self._h.emit("on_audio", data)

        class Call(pj.Call):
            def __init__(self, account, call_id=pj.PJSUA_INVALID_ID):
                super().__init__(account, call_id)

            def onCallState(self, prm):  # noqa: N802
                stack._on_state(self)

            def onCallMediaState(self, prm):  # noqa: N802
                stack._on_media(self)

        class Account(pj.Account):
            def onIncomingCall(self, prm):  # noqa: N802
                stack._on_incoming_call(self, prm)

        self._Port, self._Call = Port, Call
        acfg = pj.AccountConfig()
        acfg.idUri = f"sip:{self.cfg.sip_user}@{self.cfg.bind_address}"  # без регистрации: regUri не задан
        self.account = Account()
        self.account.create(acfg)
        log.info("SIP готов: sip:%s@%s:%d", self.cfg.sip_user, self.cfg.bind_address, self.cfg.sip_port)

    def stop(self) -> None:
        try:
            if self.ep is not None:
                self.ep.libDestroy()
        except Exception as exc:  # noqa: BLE001
            log.warning("libDestroy: %s", type(exc).__name__)

    def _configure_codecs(self) -> None:
        enabled = []
        for info in self.ep.codecEnum2():
            priority = next((p for prefix, p in CODEC_PRIORITY if info.codecId.startswith(prefix)), 0)
            self.ep.codecSetPriority(info.codecId, priority)
            if priority:
                enabled.append(info.codecId)
        log.info("Кодеки: %s", ", ".join(enabled) or "нет")
        if not any(c.startswith("opus/") for c in enabled):
            log.info("Кодек opus в этой сборке PJSIP отсутствует: используются PCMU/PCMA")

    # ---------- звук ----------

    def _to_bytevec(self, data: bytes) -> Any:
        """bytes -> ByteVector pjsua2 (в зависимости от сборки SWIG элементы знаковые или беззнаковые)."""
        pj = self.pj
        if self._bytevec_signed:
            try:
                return pj.ByteVector([b - 256 if b > 127 else b for b in data])
            except (OverflowError, TypeError, ValueError):
                self._bytevec_signed = False
        return pj.ByteVector(list(data))

    def make_port(self, handle: _Handle) -> Any:
        pj = self.pj
        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = RATE
        fmt.channelCount = 1
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = FRAME_MS * 1000
        port = self._Port(handle)
        port.createPort(f"kimo-{handle.call_id}", fmt)
        return port

    # ---------- события pjsip (потоки pjsip) ----------

    def _on_incoming_call(self, account: Any, prm: Any) -> None:
        pj = self.pj
        call = self._Call(account, prm.callId)
        src = _host(prm.rdata.srcAddress)
        decision = self.gate.decide(src, "INVITE", extract_authorization(prm.rdata.wholeMsg))
        op = pj.CallOpParam()
        op.statusCode = decision.code
        if decision.www_authenticate:
            header = pj.SipHeader()
            header.hName = "WWW-Authenticate"
            header.hValue = decision.www_authenticate
            op.txOption.headers.append(header)
        info = call.getInfo()
        log.info("Входящий INVITE от %s: %d (%s)", src, decision.code, decision.reason)
        self._keep[info.id] = call
        if decision.code == 200:
            handle = _Handle(self, "in", src)
            handle.call_id, handle.pj_call = str(info.id), call
            self._calls[info.id] = handle
            self.loop.call_soon_threadsafe(self._attach_handler, handle)
        call.answer(op)

    def _attach_handler(self, handle: _Handle) -> None:
        try:
            handle.bind(self._on_incoming(handle))
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать обработчик звонка")
            handle.hangup()

    def _on_state(self, call: Any) -> None:
        pj = self.pj
        info = call.getInfo()
        handle = self._calls.get(info.id)
        if info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            if handle is not None and not handle.ended:
                handle.ended = True
                reason = f"{info.lastStatusCode} {info.lastReason}".strip()
                handle.emit("on_ended", reason)
            self._calls.pop(info.id, None)
            self.loop.call_soon_threadsafe(self.loop.call_later, 2.0, self._keep.pop, info.id, None)
            return
        if handle is None:
            return
        if info.state == pj.PJSIP_INV_STATE_EARLY and info.lastStatusCode in (180, 183):
            handle.emit("on_early")
        elif info.state == pj.PJSIP_INV_STATE_CONFIRMED:
            handle.emit("on_connected")

    def _on_media(self, call: Any) -> None:
        pj = self.pj
        info = call.getInfo()
        handle = self._calls.get(info.id)
        if handle is None:
            return
        for index, media in enumerate(info.media):
            if media.type == pj.PJMEDIA_TYPE_AUDIO and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                handle.attach_media(call.getAudioMedia(index))

    # ---------- исходящий звонок ----------

    def call_out(self, uri: str, handler: CallHandler) -> SipCall:
        pj = self.pj
        call = self._Call(self.account)
        handle = _Handle(self, "out", uri)
        handle.pj_call = call
        handle.bind(handler)
        call.makeCall(uri, pj.CallOpParam(True))
        info = call.getInfo()
        handle.call_id = str(info.id)
        self._calls[info.id] = handle
        self._keep[info.id] = call
        return handle
