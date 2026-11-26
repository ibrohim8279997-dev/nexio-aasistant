"""Telegram Business: Кимо отвечает клиентам владельца как AI-агент (и команды управления в личке)."""
from __future__ import annotations

import io
import logging
import time
from datetime import timedelta
from html import escape
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from bot.ai.groq_client import GroqError
from bot.ai.queue import P_CLIENT, UserLimiter
from bot.config import Settings
from bot.db import Database
from bot.middlewares import PermMiddleware
from bot.services import business as biz
from bot.services import perms as perms_svc
from bot.services import subscription as sub
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)
router = Router(name="business")
router.message.middleware(PermMiddleware("business"))  # только для команд в личке; бизнес-события идут отдельно

MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_VOICE_SECONDS = 180
NOTIFY_EVERY = 30 * 60  # одно уведомление о передаче в чате не чаще раза в 30 минут

flood = biz.FloodGuard()
_notified: dict[tuple[int, int], float] = {}
_error_notified: dict[int, float] = {}
_budgets: dict[int, UserLimiter] = {}


def reset_state() -> None:
    """Сброс счётчиков в памяти (тесты, перезапуск логики)."""
    flood._hits.clear()
    _notified.clear()
    _error_notified.clear()
    _budgets.clear()


def budget_for(settings: Settings) -> UserLimiter:
    """Общий для процесса лимит ответов клиентам на владельца (ключ — owner_id)."""
    if 0 not in _budgets:
        _budgets[0] = UserLimiter(settings.business_ai_per_min, settings.business_ai_per_day)
    return _budgets[0]


async def owner_allowed(db: Any, owner: dict[str, Any], settings: Settings) -> bool:
    """Есть ли у владельца подключения доступ к Кимо и право business (точка подмены на access_grants в этапе 3)."""
    if not sub.has_access(owner["tg_id"], owner["sub_until"], settings.admin_id):
        return False
    return await perms_svc.can(db, owner, "business", settings.admin_id)


def owner_display(rules: dict[str, Any] | None, owner: dict[str, Any]) -> str:
    return ((rules or {}).get("owner_name") or owner.get("first_name") or "").strip()


# ---------- подключение ----------

@router.business_connection()
async def on_connection(event: BusinessConnection, bot: Bot, db: Database, settings: Settings) -> None:
    owner = await db.get_or_create_user(event.user.id, event.user.username, event.user.first_name, to_db(now_utc()))
    can_reply = bool(event.rights.can_reply) if event.rights is not None else bool(getattr(event, "can_reply", False))
    await db.bconn_upsert(event.id, owner["id"], event.user_chat_id, can_reply, event.is_enabled, to_db(now_utc()))
    if not await owner_allowed(db, owner, settings):
        text = "🔒 Business подключён, но доступа к Кимо у вас нет. Доступ выдаёт владелец."
    elif not event.is_enabled:
        text = "⏸ Подключение Business отключено."
    elif not can_reply:
        text = ("⚠️ Business подключён без права отвечать. В настройках Telegram Business → Чат-боты "
                "включите «Отвечать на сообщения».")
    else:
        text = ("✅ Business подключён. Кимо отвечает клиентам как AI-агент. "
                "Правила: /brules, пауза: /pause, чаты: /bchats.")
    try:
        await bot.send_message(event.user_chat_id, text, parse_mode=None)
    except TelegramAPIError as exc:
        log.warning("Не удалось сообщить о подключении Business: %s", type(exc).__name__)


# ---------- сообщения клиентов и владельца ----------

async def _notify_owner(bot: Bot, owner: dict[str, Any], text: str) -> None:
    try:
        await bot.send_message(owner["tg_id"], text, parse_mode=None)
    except TelegramAPIError as exc:
        log.warning("Не удалось уведомить владельца: %s", type(exc).__name__)


async def _client_text(message: Message, bot: Bot, groq: Any, owner_id: int) -> str | None:
    if message.text:
        return message.text
    media = message.voice
    if media is None:
        return message.caption or None
    if (media.file_size or 0) > MAX_VOICE_BYTES or (media.duration or 0) > MAX_VOICE_SECONDS:
        return None
    buffer = io.BytesIO()
    await bot.download(media, destination=buffer)
    bound = groq.bound(P_CLIENT) if hasattr(groq, "bound") else groq
    return (await bound.transcribe(buffer.getvalue(), filename="voice.ogg")).strip() or None


@router.business_message()
async def on_business_message(message: Message, bot: Bot, db: Database, settings: Settings, groq: Any) -> None:
    conn = await db.bconn_get(message.business_connection_id or "")
    if conn is None or message.chat.type != "private":
        return  # неизвестное подключение, группы и каналы — молчим
    owner = await db.get_user(conn["owner_id"])
    sender = message.from_user
    if owner is None or sender is None or sender.is_bot:
        return
    now = to_db(now_utc())
    chat_id = message.chat.id

    if sender.id == owner["tg_id"]:  # владелец написал сам
        await db.bchat_touch(owner["id"], chat_id, None, None, now, incoming=False)
        if await db.bmsg_is_ours(owner["id"], chat_id, message.message_id):
            return
        rules = await db.brules_get(owner["id"])
        hours = int((rules or {}).get("pause_hours", biz.DEFAULT_PAUSE_HOURS))
        until = to_db(now_utc() + timedelta(hours=hours)) if hours > 0 else None
        await db.bchat_set(owner["id"], chat_id, last_owner_at=now, **({"paused_until": until} if until else {}))
        await db.bmsg_add(owner["id"], chat_id, "owner", (message.text or message.caption or "[медиа]")[:biz.MAX_CLIENT_TEXT],
                          message.message_id, now)
        return

    chat = await db.bchat_touch(owner["id"], chat_id, message.chat.full_name, message.chat.username, now, incoming=True)
    try:
        text = await _client_text(message, bot, groq, owner["id"])
    except (GroqError, TelegramAPIError) as exc:
        log.info("Business: не удалось получить текст сообщения: %s", type(exc).__name__)
        return
    if not text:
        return
    await db.bmsg_add(owner["id"], chat_id, "client", text[:biz.MAX_CLIENT_TEXT], message.message_id, now)

    rules = await db.brules_get(owner["id"])
    if not await owner_allowed(db, owner, settings) or not biz.decide(conn, chat, rules).reply:
        return
    if not flood.allow(owner["id"], chat_id, int((rules or {}).get("flood_per_min", biz.DEFAULT_FLOOD_PER_MIN))):
        log.info("Business: антифлуд в чате %s", chat_id)
        return

    lang = biz.guess_lang(text)
    name = owner_display(rules, owner)
    reply: biz.Reply | None
    if biz.is_ai_question(text):
        reply = biz.Reply(biz.ai_answer_text(name, lang))
    elif biz.wants_human(text):
        reply = biz.Reply(biz.handoff_text(lang), handoff=True, reason="клиент просит человека")
    else:
        reply = await _ask_model(bot, db, settings, groq, owner, rules, chat_id, name)
        if reply is None and chat["intro_sent"]:
            return
        if reply is None:  # первое сообщение, модель недоступна: хотя бы представляемся
            reply = biz.Reply(biz.intro_text(name, lang))

    out = reply.text
    if not chat["intro_sent"] and out != biz.intro_text(name, lang) and not biz.is_ai_question(text):
        out = f"{biz.intro_head(name, lang)}\n\n{out}"  # первое сообщение в чате: представляемся
    try:
        sent = await bot.send_message(chat_id, out, business_connection_id=conn["connection_id"], parse_mode=None)
    except TelegramAPIError as exc:
        log.warning("Business: не удалось отправить ответ: %s", type(exc).__name__)
        return
    await db.bmsg_add(owner["id"], chat_id, "assistant", out, sent.message_id, to_db(now_utc()))
    await db.bchat_set(owner["id"], chat_id, intro_sent=1)
    if reply.handoff:
        await _handoff(bot, db, owner, rules, message, text, reply.reason)


async def _ask_model(bot: Bot, db: Database, settings: Settings, groq: Any, owner: dict[str, Any],
                     rules: dict[str, Any] | None, chat_id: int, name: str) -> biz.Reply | None:
    try:
        budget_for(settings).check(owner["id"])
    except GroqError:
        await _error_notice(bot, owner, "достигнут дневной/минутный лимит ответов клиентам")
        return None
    history = await db.bmsg_recent(owner["id"], chat_id, biz.CONTEXT_MESSAGES)
    messages = biz.build_messages(rules, name, history)
    try:
        bound = groq.bound(P_CLIENT) if hasattr(groq, "bound") else groq
        raw = await bound.chat_json(messages, max_tokens=700)
    except GroqError as exc:
        log.info("Business: модель недоступна: %s", type(exc).__name__)
        await _error_notice(bot, owner, "ИИ сейчас недоступен (лимит Groq)")
        return None
    return biz.parse_reply(raw)


async def _error_notice(bot: Bot, owner: dict[str, Any], why: str) -> None:
    last = _error_notified.get(owner["id"], 0.0)
    if time.monotonic() - last < 600:
        return
    _error_notified[owner["id"]] = time.monotonic()
    await _notify_owner(bot, owner, f"⚠️ Кимо не смог ответить клиенту: {why}. Ответьте сами.")


async def _handoff(bot: Bot, db: Database, owner: dict[str, Any], rules: dict[str, Any] | None, message: Message,
                   text: str, reason: str) -> None:
    chat_id = message.chat.id
    hours = int((rules or {}).get("pause_hours", biz.DEFAULT_PAUSE_HOURS))
    if hours > 0:  # пока владелец не ответит, Кимо в этом чате молчит
        await db.bchat_set(owner["id"], chat_id, paused_until=to_db(now_utc() + timedelta(hours=hours)))
    key = (owner["id"], chat_id)
    if time.monotonic() - _notified.get(key, 0.0) < NOTIFY_EVERY:
        return
    _notified[key] = time.monotonic()
    who = message.chat.full_name + (f" (@{message.chat.username})" if message.chat.username else "")
    await _notify_owner(
        bot, owner,
        f"👤 Нужен ответ владельца\nКлиент: {who}\nСообщение: «{text[:300]}»\n"
        f"{('Причина: ' + reason) if reason else ''}\nОткрыть чат: tg://user?id={chat_id}",
    )


@router.edited_business_message()
async def on_edited(message: Message, db: Database) -> None:
    conn = await db.bconn_get(message.business_connection_id or "")
    if conn is None or message.text is None:
        return
    await db.bmsg_edit(conn["owner_id"], message.chat.id, message.message_id, message.text[:biz.MAX_CLIENT_TEXT])


@router.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted, db: Database) -> None:
    conn = await db.bconn_get(event.business_connection_id)
    if conn is None:
        return
    await db.bmsg_delete(conn["owner_id"], event.chat.id, list(event.message_ids))


# ---------- команды владельца в личке ----------

RULE_FIELDS = {
    "prompt": "system_prompt", "info": "business_info", "ban": "prohibitions", "tone": "tone", "name": "owner_name",
    "pause": "pause_hours", "flood": "flood_per_min",
}
RULE_TITLES = {
    "prompt": "Правила", "info": "О бизнесе", "ban": "Запреты", "tone": "Тон", "name": "Имя владельца",
    "pause": "Пауза после ручного ответа, ч", "flood": "Ответов на чат в минуту",
}
MAX_RULE_LEN = 3000


def rules_text(rules: dict[str, Any] | None, owner: dict[str, Any]) -> str:
    r = rules or {}
    lines = ["<b>Правила Business-чата</b>"]
    values = {
        "prompt": r.get("system_prompt") or "—", "info": r.get("business_info") or "—", "ban": r.get("prohibitions") or "—",
        "tone": r.get("tone") or "—", "name": r.get("owner_name") or f"{owner.get('first_name') or '—'} (из профиля)",
        "pause": r.get("pause_hours", biz.DEFAULT_PAUSE_HOURS), "flood": r.get("flood_per_min", biz.DEFAULT_FLOOD_PER_MIN),
    }
    for key, title in RULE_TITLES.items():
        lines.append(f"• <b>{title}</b> (<code>{key}</code>): {escape(str(values[key])[:400])}")
    lines.append("\nИзменить: <code>/brules prompt Текст…</code> (поля: " + ", ".join(RULE_FIELDS) + ")")
    return "\n".join(lines)


@router.message(Command("brules"))
async def cmd_brules(message: Message, command: CommandObject, db: Database, db_user: dict[str, Any]) -> None:
    args = (command.args or "").strip()
    if args:
        key, _, value = args.partition(" ")
        key, value = key.lower(), value.strip()
        field = RULE_FIELDS.get(key)
        if field is None or (not value and key not in ("prompt", "info", "ban", "tone", "name")):
            await message.answer("Формат: /brules <поле> <текст>. Поля: " + ", ".join(RULE_FIELDS))
            return
        if len(value) > MAX_RULE_LEN:
            await message.answer(f"Слишком длинно (максимум {MAX_RULE_LEN} символов).")
            return
        stored: Any = value
        if key in ("pause", "flood"):
            if not value.isdigit() or not (0 <= int(value) <= (168 if key == "pause" else 30)):
                await message.answer("Нужно число: пауза 0–168 часов, ответов в минуту 0–30.")
                return
            stored = int(value)
        await db.brules_set(db_user["id"], to_db(now_utc()), **{field: stored})
        await message.answer("✅ Сохранено.")
    await message.answer(rules_text(await db.brules_get(db_user["id"]), db_user))


@router.message(Command("pause", "resume"))
async def cmd_pause(message: Message, command: CommandObject, db: Database, db_user: dict[str, Any]) -> None:
    pausing = (command.command or "") == "pause"
    arg = (command.args or "").strip()
    if arg:
        if not arg.lstrip("-").isdigit() or await db.bchat_get(db_user["id"], int(arg)) is None:
            await message.answer("Не нашёл такой чат. Список: /bchats")
            return
        await db.bchat_set(db_user["id"], int(arg), excluded=int(pausing), **({} if pausing else {"paused_until": None}))
        await message.answer(f"{'⏸ Кимо молчит' if pausing else '▶️ Кимо отвечает'} в чате {arg}.")
        return
    changed = await db.bconn_set_ai(db_user["id"], not pausing)
    if not changed:
        await message.answer("Business ещё не подключён. Telegram → Настройки → Telegram Business → Чат-боты.")
        return
    await message.answer("⏸ Кимо молчит во всех Business-чатах. Вернуть: /resume" if pausing
                         else "▶️ Кимо снова отвечает клиентам.")


@router.message(Command("bchats"))
async def cmd_bchats(message: Message, db: Database, db_user: dict[str, Any]) -> None:
    chats = await db.bchat_list(db_user["id"], 30)
    if not chats:
        await message.answer("Чатов клиентов пока нет: они появятся после первого сообщения клиента.")
        return
    lines = ["<b>Чаты клиентов</b>"]
    for c in chats:
        state = "🔇 исключён" if c["excluded"] else ("⏸ пауза" if biz.is_paused(c) else "✅")
        who = escape(c["client_name"] or "—") + (f" @{escape(c['username'])}" if c["username"] else "")
        lines.append(f"{state} <code>{c['chat_id']}</code> {who}")
    lines.append("\n/pause &lt;chat_id&gt; — молчать в чате, /resume &lt;chat_id&gt; — вернуть")
    await message.answer("\n".join(lines))
