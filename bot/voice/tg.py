"""Голосовые ответы в Telegram: owner отправляет голосовое — получает текст и озвученный ответ (ogg opus).

Отдельно от звонка: общаться голосом можно и в Telegram, не включая звонок."""
from __future__ import annotations

import logging
import os

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, Message

from bot.voice.tts import Synthesizer, TtsUnavailable, spoken

log = logging.getLogger("voice.tg")


async def voice_reply(message: Message, text: str, synth: Synthesizer | None) -> bool:
    """Отправить озвученный ответ голосовым сообщением. Ошибки озвучки не мешают текстовому ответу."""
    if synth is None or not text.strip():
        return False
    speech = spoken(text)
    try:
        ogg = await synth.ogg(speech.text, speech.lang)
    except TtsUnavailable as exc:
        log.warning("Голосовой ответ не создан: %s", exc)
        return False
    try:
        await message.answer_voice(BufferedInputFile(ogg, filename="reply.ogg"))
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить голосовой ответ: %s", type(exc).__name__)
        return False
    return True


def synthesizer_from_env() -> Synthesizer | None:
    """Синтезатор для голосовых ответов в Telegram. VOICE_TG_REPLY=false отключает их."""
    if os.getenv("VOICE_TG_REPLY", "true").strip().lower() in {"0", "false", "no", "off", "нет"}:
        return None
    return Synthesizer(os.getenv("VOICE_RU", "svetlana").strip().lower(), os.getenv("VOICE_UZ", "madina").strip().lower())

