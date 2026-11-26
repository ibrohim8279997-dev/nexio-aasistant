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

from bot.agents.context import AgentContext
from bot.ai.groq_client import GroqClient, GroqError
from bot.config import Settings
from bot.middlewares import PermMiddleware
from bot.db import Database
from bot.services import assistant, pipeline
from bot.services import perms as perms_svc
from bot.services.rates import RatesService
from bot.agents.bus import plain_text
from bot.voice.tg import voice_reply

log = logging.getLogger(__name__)
router = Router(name="assistant")
router.message.middleware(PermMiddleware("assistant"))
router.callback_query.middleware(PermMiddleware("assistant"))

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


def llm_for(groq: Any, db_user: dict[str, Any], settings: Settings) -> Any:
    """Обычному пользователю — клиентский приоритет и лимит обращений на него; владельцу — без ограничений."""
    if db_user["tg_id"] == settings.admin_id or not hasattr(groq, "bound"):
        return groq
    from bot.ai.queue import P_CLIENT

    return groq.bound(P_CLIENT, owner=db_user["id"])


async def make_deps(
    message: Message,
    db: Database,
    db_user: dict[str, Any],
    rates: RatesService,
    agent_ctx: AgentContext,
    settings: Settings,
) -> assistant.AssistantDeps:
    return assistant.AssistantDeps(
        perms=await perms_svc.effective_perms(db, db_user, settings.admin_id),
        db=db,
        user=db_user,
        rates=rates,
        identity=await agent_ctx.identity(),
        ctx=agent_ctx,
        chat_id=message.chat.id,
        reply_to=message.message_id,
        is_group=is_group(message),
        is_owner=bool(message.from_user and message.from_user.id == settings.admin_id),
        offer_restart=agent_ctx.admin.offer_restart if agent_ctx.admin else None,
        admin=agent_ctx.admin,
    )


async def process_text(message: Message, text: str, bot: Bot, groq: GroqClient, deps: assistant.AssistantDeps) -> list[str]:
    """Обработать фразу и ответить. Возвращает тексты ответов (для голосового ответа в Telegram)."""
    text = text.strip()
    if not text:
        return []
    if len(text) > MAX_TEXT_LEN:
        await send(message, f"Сообщение слишком длинное (максимум {MAX_TEXT_LEN} символов).")
        return []

    await _typing(bot, message.chat.id)
    try:
        answers = await pipeline.interpret(text, groq, deps)
    except GroqError as exc:
        if deps.ctx is not None:
            deps.ctx.state.set_error(f"Groq: {type(exc).__name__}")
        await send(message, exc.user_message)
        return []
    except pipeline.NotUnderstood:
        await send(message, NOT_UNDERSTOOD)
        return []
    for answer in answers:
        await send(message, answer.text, answer.markup)
    return [plain_text(a.text) for a in answers]


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
    synth: Any = None,
) -> None:
    await state.clear()
    groq = llm_for(groq, db_user, settings)
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
    replies = await process_text(message, text, bot, groq, await make_deps(message, db, db_user, rates, agent_ctx, settings))
    if replies:  # голосом можно общаться и в Telegram: текст выше, озвученный ответ ниже
        await voice_reply(message, "\n".join(replies), synth)


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
    await process_text(message, message.text or "", bot, llm_for(groq, db_user, settings), deps)
