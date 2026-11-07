"""Голос и свободный текст -> Groq -> действия."""
from __future__ import annotations

import io
import logging
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.ai.actions import ActionParseError, build_messages, parse_actions
from bot.ai.groq_client import GroqClient, GroqError
from bot.db import Database
from bot.services import assistant
from bot.services.rates import RatesService
from bot.services.tasks import open_tasks_for_prompt
from bot.timeutil import context_now_text, now_local

log = logging.getLogger(__name__)
router = Router(name="assistant")

MAX_TEXT_LEN = 1000
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API не отдаёт файлы больше 20 МБ

NOT_UNDERSTOOD = (
    "🤔 Не понял. Попробуйте иначе, например:\n"
    "• «завтра в 10:00 встреча с клиентом»\n"
    "• «такси 25 тысяч»\n"
    "• «покажи расходы за неделю»"
)


async def _typing(bot: Bot, chat_id: int) -> None:
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except TelegramAPIError:
        pass


async def process_text(message: Message, text: str, bot: Bot, groq: GroqClient, deps: assistant.AssistantDeps) -> None:
    text = text.strip()
    if not text:
        return
    if len(text) > MAX_TEXT_LEN:
        await message.answer(f"Сообщение слишком длинное (максимум {MAX_TEXT_LEN} символов).")
        return

    await _typing(bot, message.chat.id)
    open_tasks = await open_tasks_for_prompt(deps.db, deps.user["id"])
    try:
        raw = await groq.chat_json(build_messages(text, context_now_text(), open_tasks))
        actions = parse_actions(raw, now_local().date())
    except GroqError as exc:
        await message.answer(exc.user_message)
        return
    except ActionParseError as exc:
        log.info("Не удалось разобрать ответ модели: %s", exc)
        await message.answer(NOT_UNDERSTOOD)
        return

    if not actions:
        await message.answer(NOT_UNDERSTOOD)
        return
    for answer in await assistant.execute(actions, deps):
        await message.answer(answer.text, reply_markup=answer.markup)


@router.message(F.voice | F.audio)
async def on_voice(
    message: Message,
    bot: Bot,
    state: FSMContext,
    groq: GroqClient,
    db: Database,
    rates: RatesService,
    db_user: dict[str, Any],
) -> None:
    await state.clear()
    media = message.voice or message.audio
    if media is None:
        return
    if media.file_size and media.file_size > TG_DOWNLOAD_LIMIT:
        await message.answer("Голосовое слишком большое (Telegram отдаёт ботам файлы до 20 МБ).")
        return

    await _typing(bot, message.chat.id)
    buffer = io.BytesIO()
    try:
        await bot.download(media.file_id, destination=buffer)
    except TelegramAPIError as exc:
        log.warning("Не удалось скачать голосовое: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось скачать голосовое. Попробуйте ещё раз.")
        return

    filename = "voice.ogg" if message.voice else (getattr(media, "file_name", None) or "audio.mp3")
    try:
        text = await groq.transcribe(buffer.getvalue(), filename=filename)
    except GroqError as exc:
        await message.answer(exc.user_message)
        return
    if not text:
        await message.answer("🎙 Не расслышал. Попробуйте записать ещё раз.")
        return

    await message.answer(f"🎙 <i>{escape(text[:MAX_TEXT_LEN])}</i>")
    await process_text(message, text, bot, groq, assistant.AssistantDeps(db=db, user=db_user, rates=rates))


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_text(
    message: Message, bot: Bot, groq: GroqClient, db: Database, rates: RatesService, db_user: dict[str, Any]
) -> None:
    await process_text(message, message.text or "", bot, groq, assistant.AssistantDeps(db=db, user=db_user, rates=rates))
