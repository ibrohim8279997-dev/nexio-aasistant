"""Конец фразы: детектор речи (webrtcvad) и сегментатор. Пауза около 0.8 с — конец реплики, минимум 0.4 с речи."""
from __future__ import annotations

import logging
import math
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from bot.voice.stack import FRAME_BYTES, FRAME_MS

log = logging.getLogger("voice.vad")

END_SILENCE_MS = 800
MIN_SPEECH_MS = 400
START_MS = 100  # столько речи подряд считаем началом фразы (и перебиванием при озвучке)
PREROLL_MS = 200
MAX_UTTERANCE_MS = 15000
TAIL_KEEP_MS = 200


def rms(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


class EnergyVad:
    """Запасной детектор по громкости с адаптивным порогом (если webrtcvad не установлен и в тестах)."""

    def __init__(self, min_rms: float = 350.0, ratio: float = 3.0) -> None:
        self.min_rms, self.ratio = min_rms, ratio
        self._floor = 100.0

    def __call__(self, frame: bytes) -> bool:
        level = rms(frame)
        threshold = max(self.min_rms, self._floor * self.ratio)
        speech = level > threshold
        if not speech:  # фон оцениваем только по тишине
            self._floor = 0.95 * self._floor + 0.05 * level
        return speech


def make_vad(aggressiveness: int = 2) -> Callable[[bytes], bool]:
    """webrtcvad, если он установлен, иначе энергетический детектор."""
    try:
        import webrtcvad  # noqa: PLC0415

        vad = webrtcvad.Vad(aggressiveness)
        return lambda frame: vad.is_speech(frame, 16000)
    except Exception as exc:  # noqa: BLE001 — нет пакета или pkg_resources
        log.warning("webrtcvad недоступен (%s): использую детектор по громкости", type(exc).__name__)
        return EnergyVad()


@dataclass(frozen=True)
class SpeechStart:
    pass


@dataclass(frozen=True)
class Utterance:
    pcm: bytes
    speech_ms: int


class Segmenter:
    """Кадры по 20 мс -> события SpeechStart и Utterance (законченная фраза)."""

    def __init__(self, is_speech: Callable[[bytes], bool], end_silence_ms: int = END_SILENCE_MS,
                 min_speech_ms: int = MIN_SPEECH_MS, start_ms: int = START_MS, preroll_ms: int = PREROLL_MS,
                 max_ms: int = MAX_UTTERANCE_MS) -> None:
        self._is_speech = is_speech
        self._end = end_silence_ms // FRAME_MS
        self._min_speech = min_speech_ms // FRAME_MS
        self._start = max(start_ms // FRAME_MS, 1)
        self._max = max_ms // FRAME_MS
        self._preroll: deque[bytes] = deque(maxlen=preroll_ms // FRAME_MS + self._start)
        self._buf: list[bytes] = []
        self._speaking = False
        self._run = 0  # подряд идущие кадры речи (до начала фразы)
        self._silence = 0
        self._voiced = 0

    def reset(self) -> None:
        self._preroll.clear()
        self._buf = []
        self._speaking = False
        self._run = self._silence = self._voiced = 0

    def feed(self, frame: bytes) -> list[SpeechStart | Utterance]:
        if len(frame) != FRAME_BYTES:
            return []
        voiced = self._is_speech(frame)
        events: list[SpeechStart | Utterance] = []
        if not self._speaking:
            self._preroll.append(frame)
            self._run = self._run + 1 if voiced else 0
            if self._run >= self._start:
                self._speaking = True
                self._buf = list(self._preroll)
                self._voiced = self._run
                self._silence = 0
                events.append(SpeechStart())
            return events
        self._buf.append(frame)
        if voiced:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self._end or len(self._buf) >= self._max:
            events.extend(self._finish())
        return events

    def _finish(self) -> list[Utterance]:
        voiced, buf, tail = self._voiced, self._buf, self._silence
        self.reset()
        if voiced < self._min_speech:
            return []  # короче 0.4 с: шум, на распознавание не отправляем
        keep = max(len(buf) - tail + TAIL_KEEP_MS // FRAME_MS, 0)
        return [Utterance(b"".join(buf[:keep]), voiced * FRAME_MS)]
