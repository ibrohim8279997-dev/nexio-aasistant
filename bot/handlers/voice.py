"""Голос и свободный текст -> Groq -> действия."""
from __future__ import annotations

import io
import logging
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.ai.actions import ActionParseError, build_messages, parse_actions
from bot.agents.context import AgentContext
from bot.ai.groq_client import GroqClient, GroqError
from bot.config import Settings
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


def is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def send(message: Message, text: str, markup: Any = None) -> None:
    """В группе — реплаем на исходное сообщение, в личке — обычным сообщением."""
    if is_group(message):
        await message.reply(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def make_deps(
    message: Message,
    db: Database,
    db_user: dict[str, Any],
    rates: RatesService,
    agent_ctx: AgentContext,
    settings: Settings,
) -> assistant.AssistantDeps:
    return assistant.AssistantDeps(
        db=db,
        user=db_user,
        rates=rates,
        identity=await agent_ctx.identity(),
        ctx=agent_ctx,
        chat_id=message.chat.id,
        reply_to=message.message_id,
        is_group=is_group(message),
        is_owner=bool(message.from_user and message.from_user.id == settings.admin_id),
    )


async def process_text(message: Message, text: str, bot: Bot, groq: GroqClient, deps: assistant.AssistantDeps) -> None:
    text = text.strip()
    if not text:
        return
    if len(text) > MAX_TEXT_LEN:
        await send(message, f"Сообщение слишком длинное (максимум {MAX_TEXT_LEN} символов).")
        return

    await _typing(bot, message.chat.id)
    open_tasks = await open_tasks_for_prompt(deps.db, deps.user["id"])
    # Передавать задачи может только владелец: агенты — его помощники
    agents = None
    if deps.ctx is not None and deps.is_owner:
        agents = {a.name for a in await deps.ctx.registry.others(deps.ctx.agent.name)}
    try:
        raw = await groq.chat_json(build_messages(text, context_now_text(), open_tasks, deps.identity))
        actions = parse_actions(raw, now_local().date(), agents)
    except GroqError as exc:
        await send(message, exc.user_message)
        return
    except ActionParseError as exc:
        log.info("Не удалось разобрать ответ модели: %s", exc)
        await send(message, NOT_UNDERSTOOD)
        return

    if not actions:
        await send(message, NOT_UNDERSTOOD)
        return
    for answer in await assistant.execute(actions, deps):
        await send(message, answer.text, answer.markup)


@router.message(F.voice | F.audio)
async def on_voice(
    message: Message,
    bot: Bot,
    state: FSMContext,
    groq: GroqClient,
    db: Database,
    rates: RatesService,
    db_user: dict[str, Any],
    agent_ctx: AgentContext,
    settings: Settings,
) -> None:
    await state.clear()
    media = message.voice or message.audio
    if media is None:
        return
    if media.file_size and media.file_size > TG_DOWNLOAD_LIMIT:
        await send(message, "Голосовое слишком большое (Telegram отдаёт ботам файлы до 20 МБ).")
        return

    await _typing(bot, message.chat.id)
    buffer = io.BytesIO()
    try:
        await bot.download(media.file_id, destination=buffer)
    except TelegramAPIError as exc:
        log.warning("Не удалось скачать голосовое: %s", type(exc).__name__)
        await send(message, "⚠️ Не удалось скачать голосовое. Попробуйте ещё раз.")
        return

    filename = "voice.ogg" if message.voice else (getattr(media, "file_name", None) or "audio.mp3")
    try:
        text = await groq.transcribe(buffer.getvalue(), filename=filename)
    except GroqError as exc:
        await send(message, exc.user_message)
        return
    if not text:
        await send(message, "🎙 Не расслышал. Попробуйте записать ещё раз.")
        return

    await send(message, f"🎙 <i>{escape(text[:MAX_TEXT_LEN])}</i>")
    await process_text(message, text, bot, groq, await make_deps(message, db, db_user, rates, agent_ctx, settings))


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_text(
    message: Message,
    bot: Bot,
    groq: GroqClient,
    db: Database,
    rates: RatesService,
    db_user: dict[str, Any],
    agent_ctx: AgentContext,
    settings: Settings,
) -> None:
    deps = await make_deps(message, db, db_user, rates, agent_ctx, settings)
    await process_text(message, message.text or "", bot, groq, deps)
