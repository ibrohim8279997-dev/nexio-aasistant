"""Telegram Business: Кимо отвечает клиентам владельца как AI-агент (и команды управления в личке)."""
from __future__ import annotations

import io
import logging
import time
from datetime import timedelta
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from bot.ai.groq_client import GroqError
from bot.ai.queue import P_CLIENT, UserLimiter
from bot.config import Settings
from bot.db import Database
from bot.middlewares import PermMiddleware
from bot.services import broadcast as bcast
from bot.services import business as biz
from bot.services import perms as perms_svc
from bot.services import grants as grants_svc
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
    if not await grants_svc.has_access(db, owner, settings):
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

    if biz.is_stop(text):  # «стоп» / «не пиши»: рассылки этому человеку отключаются навсегда
        stopped = await db.sched_disable_chat(owner["id"], chat_id)
        await db.optout_add(owner["id"], chat_id, now)
        if stopped and conn["can_reply"] and conn["is_enabled"]:
            try:
                ack = "Хорошо, больше не буду писать." if biz.guess_lang(text) == "ru" else "Xo'p, endi yozmayman."
                sent = await bot.send_message(chat_id, ack, business_connection_id=conn["connection_id"], parse_mode=None)
                await db.bmsg_add(owner["id"], chat_id, "assistant", ack, sent.message_id, to_db(now_utc()))
            except TelegramAPIError as exc:
                log.warning("Business: не удалось подтвердить «стоп»: %s", type(exc).__name__)
        return

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

RULE_FIELDS = biz.RULE_FIELDS
RULE_TITLES = {
    "prompt": "Правила", "info": "О бизнесе", "ban": "Запреты", "tone": "Тон", "name": "Имя владельца",
    "pause": "Пауза после ручного ответа, ч", "flood": "Ответов на чат в минуту",
}


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
        try:
            field, stored = biz.clean_rule(key.lower(), value.strip())
        except biz.RuleError as exc:
            await message.answer(f"⚠️ {escape(str(exc))}")
            return
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


# ---------- «стоп» от подписчика бота и рассылки (команды владельца) ----------

@router.message(F.text, F.chat.type == "private", flags={"public": True, "noperm": True})
async def on_subscriber_stop(message: Message, db: Database) -> None:
    """Подписчик, который написал боту «стоп», отписывается от рассылок. Остальным сообщениям не мешаем."""
    if not biz.is_stop(message.text or ""):
        raise SkipHandler
    owners = await db.bsub_owners_of(message.chat.id)
    if not owners:
        raise SkipHandler
    for sub_row in owners:
        await db.bsub_remove(sub_row["owner_id"], sub_row["chat_id"])
        await db.sched_disable_chat(sub_row["owner_id"], sub_row["chat_id"])
        await db.optout_add(sub_row["owner_id"], sub_row["chat_id"], to_db(now_utc()))
    await message.answer("Хорошо, больше не буду писать. Подписаться снова можно по ссылке владельца.")


BCAST_HELP = (
    "<b>Рассылки</b>\n"
    "/bcast — список\n"
    "/bcast add &lt;chat_id&gt; &lt;ЧЧ:ММ&gt; &lt;дни&gt; text &lt;текст&gt; — фиксированный текст\n"
    "/bcast add &lt;chat_id&gt; &lt;ЧЧ:ММ&gt; &lt;дни&gt; ai &lt;тема&gt; — ИИ по теме («спроси, как дела»)\n"
    "Дни: daily, weekdays, weekend, 1-5, пн,ср,пт\n"
    "/bcast on|off|del &lt;id&gt;, /bcast log, /bcast chats — кому можно писать, /bcast link — ссылка для подписки"
)


def sched_line(s: dict[str, Any]) -> str:
    what = f"текст: {s['text'][:40]}" if s["mode"] == "text" else f"ИИ: {s['topic'][:40]}"
    state = "✅" if s["active"] else "⏸"
    return (f"{state} <b>#{s['id']}</b> чат <code>{s['chat_id']}</code> {s['send_time']} дни {s['weekdays']} "
            f"({'бот' if s['channel'] == 'bot' else 'Business'}) — {escape(what)}\n    последний: {escape(s['last_status'] or '—')}")


@router.message(Command("bcast"), flags={"perm": "broadcasts"})
async def cmd_bcast(message: Message, command: CommandObject, bot: Bot, db: Database, db_user: dict[str, Any]) -> None:
    args = (command.args or "").split(maxsplit=5)
    uid = db_user["id"]
    if not args:
        rows = await db.sched_list(uid)
        await message.answer("\n".join(["<b>Рассылки</b>"] + [sched_line(s) for s in rows]) if rows
                             else "Рассылок нет.\n\n" + BCAST_HELP)
        return
    action = args[0].lower()
    if action == "add":
        if len(args) < 6 or not args[1].lstrip("-").isdigit():
            await message.answer(BCAST_HELP)
            return
        mode, payload = args[4].lower(), args[5]
        try:
            sid = await bcast.create_schedule(
                db, db_user, int(args[1]), args[2], bcast.parse_days(args[3]), mode,
                payload if mode == "text" else "", payload if mode == "ai" else "",
            )
        except bcast.BroadcastError as exc:
            await message.answer(f"⚠️ {escape(str(exc))}")
            return
        known = {c["chat_id"] for c in await bcast.known_chats(db, uid)}
        note = "" if int(args[1]) in known else (
            "\n⚠️ Кимо ещё не видел сообщений от этого чата: пока человек не напишет вам (или не подпишется по /bcast link), "
            "рассылка будет пропускаться.")
        await message.answer(f"✅ Рассылка #{sid} создана.{note}")
        return
    if action in ("on", "off", "del") and len(args) == 2 and args[1].isdigit():
        sid = int(args[1])
        if action == "del":
            ok = await db.sched_delete(uid, sid)
        else:
            sch = await db.sched_get(uid, sid)
            if sch is not None and action == "on" and await db.optout_has(uid, sch["chat_id"]):
                await message.answer("⚠️ Этот человек написал «стоп», включить нельзя.")
                return
            ok = await db.sched_update(uid, sid, active=int(action == "on"))
        await message.answer("Готово." if ok else "Не нашёл такую рассылку.")
        return
    if action == "log":
        rows = await db.blog_list(uid, 15)
        lines = [f"{r['ts'][5:16]} #{r['schedule_id']} чат {r['chat_id']}: {escape(r['status'])} {escape(r['detail'][:60])}" for r in rows]
        await message.answer("<b>История отправок</b>\n" + "\n".join(lines) if lines else "История пуста.")
        return
    if action == "chats":
        chats = await bcast.known_chats(db, uid)
        lines = [f"<code>{c['chat_id']}</code> {escape(c['name'] or '—')} ({'бот' if c['channel'] == 'bot' else 'Business'})" for c in chats]
        await message.answer("<b>Кому можно писать</b>\n" + "\n".join(lines) if lines else "Пока никого: нужны клиенты, которые писали вам, или подписчики.")
        return
    if action == "link":
        me = await bot.get_me()
        await message.answer(
            f"Ссылка для подписки (человек сам нажимает «Запустить»):\nhttps://t.me/{me.username}?start=sb_{db_user['tg_id']}\n"
            "Подписанным людям Кимо пишет от имени бота, без 24-часового окна Business. Отписка — слово «стоп».")
        return
    await message.answer(BCAST_HELP)
