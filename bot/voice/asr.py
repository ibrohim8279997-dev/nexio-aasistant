"""Распознавание фразы: wav -> Whisper на Groq. Язык не фиксируется (русский и узбекский определяются сами)."""
from __future__ import annotations

import io
import logging
import re
import wave
from typing import Any

from bot.ai.groq_client import GroqError
from bot.logging_setup import redact_text
from bot.voice.stack import RATE

log = logging.getLogger("voice.asr")

# Whisper на шуме и тишине любит «дописывать» типичные фразы из субтитров: такие результаты не передаём Кимо
HALLUCINATIONS = (
    "субтитры сделал", "субтитры создавал", "редактор субтитров", "продолжение следует", "спасибо за просмотр",
    "подписывайтесь на канал", "thank you for watching", "thanks for watching", "subtitles by",
    "tomosha qilganingiz uchun", "obuna bo'ling", "obuna bo‘ling", "amara.org",
)
MIN_TEXT_CHARS = 2


class SpeechUnavailable(Exception):
    """Распознавание недоступно (Groq): причина в лог, звонящему — «сейчас не могу ответить»."""


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def clean_transcript(text: str | None) -> str | None:
    """None — пустая фраза или типичная «галлюцинация» Whisper."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(re.sub(r"[\W_]+", "", text)) < MIN_TEXT_CHARS:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in HALLUCINATIONS):
        return None
    return text


class Recognizer:
    def __init__(self, groq: Any) -> None:
        self._groq = groq

    async def recognize(self, pcm: bytes) -> str | None:
        try:
            text = await self._groq.transcribe(pcm_to_wav(pcm), "phrase.wav")
        except GroqError as exc:
            reason = f"{type(exc).__name__}: {redact_text(str(exc))}"
            log.error("Whisper недоступен: %s", reason)
            raise SpeechUnavailable(reason) from exc
        cleaned = clean_transcript(text)
        if cleaned is None and text:
            log.info("Фраза отброшена как шум/галлюцинация Whisper (%d символов)", len(text))
        return cleaned
