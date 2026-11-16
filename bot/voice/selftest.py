"""Самопроверка на сервере: звонок самому себе по localhost с паролем и без него (Linphone не нужен).

Проверяет: привязку к адресам, digest (неверный пароль -> 403, верный -> соединение), звук в обе стороны через
AudioMediaPort, список кодеков. Перед запуском остановите voice_sip: ./run.sh stop voice_sip
Сам я эту проверку без pjsua2 запустить не мог, поэтому её вывод — первое реальное подтверждение работы связки."""
from __future__ import annotations

import asyncio
import math
import struct
from array import array
from typing import Any

from bot.voice.config import VoiceConfig
from bot.voice.digest import AttemptGuard, DigestAuthenticator, IncomingGate
from bot.voice.stack import FRAME_BYTES, FRAME_MS, RATE

TONE_SECONDS = 1.5
MIN_RMS = 500.0


def tone_frames(seconds: float = TONE_SECONDS, hz: int = 440, amplitude: int = 8000) -> list[bytes]:
    total = int(RATE * seconds)
    samples = [int(amplitude * math.sin(2 * math.pi * hz * i / RATE)) for i in range(total)]
    pcm = struct.pack(f"<{total}h", *samples)
    frames = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    return [f + b"\x00" * (FRAME_BYTES - len(f)) for f in frames]


def rms(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


class _Recorder:
    """Обработчик звонка на стороне сервера: считает принятый звук и умеет ответить тоном."""

    def __init__(self) -> None:
        self.call: Any = None
        self.connected = False
        self.ended: str | None = None
        self.loud_frames = 0

    def on_early(self) -> None: ...

    def on_connected(self) -> None:
        self.connected = True

    def on_audio(self, frame: bytes) -> None:
        if rms(frame) > MIN_RMS:
            self.loud_frames += 1

    def on_ended(self, reason: str) -> None:
        self.ended = reason


async def run_selftest(cfg: VoiceConfig) -> int:
    try:
        import pjsua2 as pj
    except ImportError as exc:
        print(f"✖ pjsua2 не импортируется: {exc}. Запускайте из окружения ~/sipenv.")
        return 2
    from bot.voice.pjsua_stack import Pjsua2Stack

    loop = asyncio.get_running_loop()
    recorder = _Recorder()

    def on_incoming(call: Any) -> _Recorder:
        recorder.call = call
        return recorder

    gate = IncomingGate(DigestAuthenticator(cfg.sip_user, cfg.sip_password, cfg.realm), AttemptGuard(), lambda: False)
    stack = Pjsua2Stack(cfg, gate, on_incoming, loop)
    stack.start()
    ep = stack.ep
    ok_all = True

    def report(ok: bool, text: str) -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        print(("✔ " if ok else "✖ ") + text)

    try:
        enabled = [c.codecId for c in ep.codecEnum2() if c.priority > 0]
        report(bool(enabled), "Кодеки: " + (", ".join(enabled) or "нет"))
        if not any(c.startswith("opus/") for c in enabled):
            print("  (opus в сборке нет — будут PCMU/PCMA, этого достаточно)")

        tcfg = pj.TransportConfig()
        tcfg.port = 0
        tcfg.boundAddress = "127.0.0.1"
        transport_id = ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)

        class ClientPort(pj.AudioMediaPort):
            def __init__(self) -> None:
                super().__init__()
                self.out = list(tone_frames())
                self.loud = 0

            def onFrameRequested(self, frame):  # noqa: N802
                if not self.out:
                    frame.type = pj.PJMEDIA_FRAME_TYPE_NONE
                    frame.size = 0
                    return
                data = self.out.pop(0)
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = stack._to_bytevec(data)
                frame.size = len(data)

            def onFrameReceived(self, frame):  # noqa: N802
                if frame.size and rms(bytes(b & 0xFF for b in list(frame.buf)[: frame.size])) > MIN_RMS:
                    self.loud += 1

        class ClientCall(pj.Call):
            def __init__(self, account) -> None:
                super().__init__(account)
                self.port: ClientPort | None = None
                self.final_code = 0
                self.confirmed = False
                self.disconnected = False

            def onCallState(self, prm):  # noqa: N802
                info = self.getInfo()
                self.final_code = info.lastStatusCode
                if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
                    self.confirmed = True
                if info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                    self.disconnected = True

            def onCallMediaState(self, prm):  # noqa: N802
                info = self.getInfo()
                for index, media in enumerate(info.media):
                    if media.type == pj.PJMEDIA_TYPE_AUDIO and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE and self.port is None:
                        fmt = pj.MediaFormatAudio()
                        fmt.type = pj.PJMEDIA_TYPE_AUDIO
                        fmt.clockRate, fmt.channelCount, fmt.bitsPerSample = RATE, 1, 16
                        fmt.frameTimeUsec = FRAME_MS * 1000
                        self.port = ClientPort()
                        self.port.createPort("selftest", fmt)
                        audio = self.getAudioMedia(index)
                        audio.startTransmit(self.port)
                        self.port.startTransmit(audio)

        def client_account(password: str) -> Any:
            acfg = pj.AccountConfig()
            acfg.idUri = "sip:tester@127.0.0.1"
            acfg.sipConfig.transportId = transport_id
            acfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", cfg.realm, cfg.sip_user, 0, password))
            acc = pj.Account()
            acc.create(acfg)
            return acc

        target = f"sip:{cfg.sip_user}@127.0.0.1:{cfg.sip_port}"

        # 1) неверный пароль -> 403
        bad_acc = client_account(cfg.sip_password + "-wrong")
        bad = ClientCall(bad_acc)
        bad.makeCall(target, pj.CallOpParam(True))
        for _ in range(60):
            if bad.disconnected:
                break
            await asyncio.sleep(0.1)
        report(bad.final_code == 403 and not recorder.connected, f"неверный пароль отклонён (код {bad.final_code}, ожидался 403)")

        # 2) верный пароль -> соединение и звук в обе стороны
        good_acc = client_account(cfg.sip_password)
        good = ClientCall(good_acc)
        good.makeCall(target, pj.CallOpParam(True))
        for _ in range(60):
            if good.confirmed and recorder.connected:
                break
            await asyncio.sleep(0.1)
        report(good.confirmed and recorder.connected, "звонок с верным паролем принят")
        if good.confirmed:
            await asyncio.sleep(TONE_SECONDS + 1.0)
            report(recorder.loud_frames > 10, f"звук клиент -> сервер дошёл ({recorder.loud_frames} громких кадров)")
            if recorder.call is not None:
                recorder.call.send_audio(b"".join(tone_frames()))
            await asyncio.sleep(TONE_SECONDS + 1.0)
            heard = good.port.loud if good.port else 0
            report(heard > 10, f"звук сервер -> клиент дошёл ({heard} громких кадров)")
            good.hangup(pj.CallOpParam(True))
            await asyncio.sleep(0.5)
    finally:
        stack.stop()
    print("ИТОГ:", "всё работает" if ok_all else "есть проблемы, пришлите вывод целиком")
    return 0 if ok_all else 1
