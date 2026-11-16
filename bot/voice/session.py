"""Сессия звонка: звук -> фразы -> Whisper -> Кимо -> озвучка -> звонок. Только звонок и звук, решений здесь нет.

Решения принимает Кимо через очередь заданий (KimoLink, bot/voice/kimo_link.py). Telegram получает тексты через Relay."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import struct
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from bot.logging_setup import redact_text
from bot.voice.asr import Recognizer, SpeechUnavailable
from bot.voice.config import VoiceConfig
from bot.voice.service import CallRecord, VoiceService
from bot.voice.stack import RATE, SipCall
from bot.voice.tts import UNAVAILABLE, Synthesizer, TtsUnavailable, spoken
from bot.voice.vad import Segmenter, SpeechStart, Utterance, make_vad

log = logging.getLogger("voice.session")

WAIT_NOTICE_AFTER = 4.0  # пока ждём Кимо дольше, говорим «секунду»
HISTORY_TURNS = 3
LATE_PHRASE = "Не успел получить ответ. Пришлю его в Telegram, как только будет готов."
FAILED_PREFIX = "Не получилось."
GOODBYE_IDLE = "Если я не нужен, до свидания."
GOODBYE_LIMIT = "Время разговора вышло. До свидания."


@dataclass
class KimoReply:
    text: str
    failed: bool = False
    timed_out: bool = False
    answered_by: str = "kimo"
    task_id: int | None = None


class KimoLink(Protocol):
    async def ask(self, text: str, history: list[tuple[str, str]], call_id: str) -> KimoReply: ...


class Relay(Protocol):
    async def send(self, text: str) -> None: ...


@dataclass
class VoiceDeps:
    asr: Recognizer
    tts: Synthesizer
    kimo: KimoLink
    relay: Relay
    cfg: VoiceConfig


def beep_pcm(hz: int = 400, ms: int = 200, pause_ms: int = 150, times: int = 2) -> bytes:
    """Сигнал ошибки, если озвучить нечем (edge-tts недоступен и заготовки нет)."""
    n = RATE * ms // 1000
    tone = struct.pack(f"<{n}h", *(int(6000 * math.sin(2 * math.pi * hz * i / RATE)) for i in range(n)))
    gap = b"\x00\x00" * (RATE * pause_ms // 1000)
    return (tone + gap) * times


class WavRecorder:
    """Запись звука звонящего в data/calls. Работает только при SIP_RECORD_CALLS=true."""

    def __init__(self, directory: Path, call_id: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{call_id}.wav"
        self._wav = wave.open(str(self.path), "wb")  # noqa: SIM115
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(RATE)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def write(self, frame: bytes) -> None:
        self._wav.writeframes(frame)

    def close(self) -> None:
        self._wav.close()


class CallSession:
    def __init__(self, call: SipCall, service: VoiceService, deps: VoiceDeps, *, opening: str | None = None,
                 vad: Callable[[bytes], bool] | None = None, wait_notice_after: float = WAIT_NOTICE_AFTER,
                 clock: Callable[[], float] = time.monotonic, record_dir: Path | None = None) -> None:
        self.call, self.service, self.deps = call, service, deps
        self.record: CallRecord | None = service.begin(call)
        self.opening = opening
        self._clock = clock
        self._wait_notice_after = wait_notice_after
        self._segmenter = Segmenter(vad or make_vad())
        self._frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._utterances: asyncio.Queue[Utterance] = asyncio.Queue()
        self._interrupt = asyncio.Event()
        self.speaking = False
        self.barge_ins = 0
        self.history: list[tuple[str, str]] = []
        self.transcript: list[tuple[str, str]] = []  # (кто, текст) — уйдёт в Telegram одним сообщением после звонка
        self.early = asyncio.Event()
        self.connected = asyncio.Event()
        self.ended = asyncio.Event()
        self.end_reason = ""
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = clock()
        self._last_activity = self._started
        self._recorder: WavRecorder | None = None
        if self.record is None:
            call.hangup()
        elif deps.cfg.record_calls:
            self._recorder = WavRecorder(record_dir or Path("data/calls"), call.call_id)

    # ---------- события от SIP-стека (поток asyncio) ----------

    def on_early(self) -> None:
        self.early.set()

    def on_connected(self) -> None:
        if self.record is None or self.connected.is_set():
            return
        self.record.connected = time.monotonic()
        self._started = self._last_activity = self._clock()
        self.connected.set()
        self._tasks.append(asyncio.get_running_loop().create_task(self._run()))

    def on_audio(self, frame: bytes) -> None:
        if self.ended.is_set():
            return
        try:
            self._frames.put_nowait(frame)
        except asyncio.QueueFull:
            pass  # отстаём: лучше потерять кадр, чем копить задержку

    def on_ended(self, reason: str) -> None:
        if self.ended.is_set():
            return
        self.end_reason = reason
        self.ended.set()
        self._interrupt.set()
        for task in self._tasks:
            task.cancel()
        if self._recorder:
            self._recorder.close()
        if self.record is not None:
            self.service.finish(self.record, reason)
            asyncio.get_running_loop().create_task(self._send_transcript())

    # ---------- основной цикл ----------

    async def _run(self) -> None:
        worker = asyncio.get_running_loop().create_task(self._process_loop())
        self._tasks.append(worker)
        try:
            if self.opening:
                await self.say(self.opening)
            elif self.call.direction == "in":
                await self.say("Слушаю.")
            while not self.ended.is_set():
                try:
                    frame = await asyncio.wait_for(self._frames.get(), 0.5)
                except asyncio.TimeoutError:
                    frame = None
                if frame is not None:
                    self._feed(frame)
                now = self._clock()
                if now - self._started > self.deps.cfg.max_call_seconds:
                    await self._goodbye(GOODBYE_LIMIT)
                    return
                if not self.speaking and now - self._last_activity > self.deps.cfg.idle_seconds:
                    await self._goodbye(GOODBYE_IDLE)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — ошибка в логике не должна оставить звонок повисшим
            log.exception("Сбой в сессии звонка %s", self.call.call_id)
            self.call.hangup()

    async def _goodbye(self, phrase: str) -> None:
        await self.say(phrase)
        self.call.hangup()

    def _feed(self, frame: bytes) -> None:
        if self._recorder:
            self._recorder.write(frame)
        for event in self._segmenter.feed(frame):
            if isinstance(event, SpeechStart):
                self._last_activity = self._clock()
                if self.speaking:  # перебивание: останавливаем озвучку и слушаем
                    self.call.clear_audio()
                    self._interrupt.set()
                    self.barge_ins += 1
                    log.info("Перебивание: озвучка остановлена")
            elif isinstance(event, Utterance):
                self._utterances.put_nowait(event)

    async def _process_loop(self) -> None:
        while True:
            utterance = await self._utterances.get()
            try:
                await self._handle(utterance)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Не удалось обработать фразу")
                await self._say_unavailable("ru")

    # ---------- одна реплика ----------

    async def _handle(self, utterance: Utterance) -> None:
        try:
            text = await self.deps.asr.recognize(utterance.pcm)
        except SpeechUnavailable:
            await self._say_unavailable("ru")  # причина уже в логе
            return
        if not text:
            return
        self._last_activity = self._clock()
        if self.record:
            self.record.phrases += 1
        reply = await self._ask_kimo(text)
        self._last_activity = self._clock()
        self.transcript.append(("Вы", text))
        if reply.timed_out:
            self.transcript.append(("Кимо", "(не успел ответить, результат пришлю отдельно)"))
            await self._play(LATE_PHRASE, "ru")
            return
        speech = spoken(reply.text)
        if reply.failed:
            self.transcript.append(("Кимо", f"(не получилось) {reply.text}"))
            await self._play(f"{FAILED_PREFIX} {speech.text}", speech.lang)
            return
        if speech.details:  # длинное, таблицы, логи, ссылки: полный текст сразу в Telegram
            await self.deps.relay.send(f"📞 {reply.text}")
            self.transcript.append(("Кимо", "(полный текст отправлен отдельным сообщением)"))
        else:
            self.transcript.append(("Кимо", reply.text))
        self.history = (self.history + [(text, reply.text)])[-HISTORY_TURNS:]
        await self._play(speech.text, speech.lang)

    async def _ask_kimo(self, text: str) -> KimoReply:
        notice = asyncio.get_running_loop().create_task(self._notice())
        try:
            return await self.deps.kimo.ask(text, list(self.history), self.call.call_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Связь с Кимо: %s", type(exc).__name__)
            return KimoReply("Кимо сейчас недоступен.", failed=True)
        finally:
            notice.cancel()

    async def _notice(self) -> None:
        await asyncio.sleep(self._wait_notice_after)
        if not self.speaking and not self.ended.is_set():
            await self._play("Секунду.", "ru")

    # ---------- озвучка ----------

    async def say(self, text: str) -> bool:
        speech = spoken(text)
        return await self._play(speech.text, speech.lang)

    async def _play(self, text: str, lang: str) -> bool:
        """Озвучить и дождаться конца воспроизведения. False — перебили или звонок закончился."""
        try:
            pcm, _voice = await self.deps.tts.pcm(text, lang)
        except TtsUnavailable as exc:
            log.error("Озвучка недоступна: %s", redact_text(str(exc)))
            await self._say_unavailable(lang, tts_down=True)
            return False
        return await self._send_pcm(pcm)

    async def _send_pcm(self, pcm: bytes) -> bool:
        if self.ended.is_set():
            return False
        self._interrupt.clear()
        self.speaking = True
        self.call.send_audio(pcm)
        try:
            while self.call.pending_ms() > 0 and not self._interrupt.is_set() and not self.ended.is_set():
                await asyncio.sleep(0.05)
        finally:
            self.speaking = False
        return not self._interrupt.is_set()

    async def _say_unavailable(self, lang: str, tts_down: bool = False) -> None:
        """«Сейчас не могу ответить»: голосом, если есть заготовка, иначе сигналом + текст в Telegram. Не молчим."""
        pcm = self.deps.tts.unavailable_pcm(lang)
        if pcm is not None and not tts_down:
            await self._send_pcm(pcm)
            return
        if pcm is not None:
            await self._send_pcm(pcm)
        else:
            await self._send_pcm(beep_pcm())
        try:
            await self.deps.relay.send("📞 " + UNAVAILABLE["ru"] + " (сервис распознавания или озвучки недоступен, подробности в логе voice_sip)")
        except Exception:  # noqa: BLE001
            log.warning("Не удалось сообщить в Telegram о сбое озвучки")

    # ---------- после звонка ----------

    async def _send_transcript(self) -> None:
        if not self.transcript or self.record is None:
            return
        started = self.record.started_wall
        seconds = round(time.monotonic() - (self.record.connected or self.record.started))
        head = f"📞 Звонок {started:%H:%M}, {seconds // 60}:{seconds % 60:02d}, {'входящий' if self.call.direction == 'in' else 'исходящий'}"
        body = "\n".join(f"— {who}: {text}" for who, text in self.transcript)
        try:
            await self.deps.relay.send(f"{head}\n{body}")
        except Exception:  # noqa: BLE001
            log.warning("Не удалось отправить расшифровку звонка в Telegram")
