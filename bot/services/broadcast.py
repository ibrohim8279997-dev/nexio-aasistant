"""Рассылки по расписанию клиентам владельца.

Ограничения Bot API, которые здесь соблюдаются, а не обходятся:
- Бот не видит список контактов владельца. Получатели — чаты, где уже были входящие business-сообщения (Кимо их запоминает),
  и люди, которые сами подписались по ссылке на бота. Ручной ID принимается, но писать по нему можно только если человек
  уже писал владельцу или подписался.
- По BusinessBotRights.can_reply бот отвечает только в личные чаты, где были входящие сообщения за последние 24 часа.
  Если окна нет или Telegram отклонил отправку, Кимо ничего не обходит: пишет причину в last_status и присылает владельцу
  готовый текст для ручной отправки.
- Для ежедневных сообщений вне 24-часового окна есть законный вариант: человек сам запускает бота по ссылке (канал bot) —
  тогда сообщения идут от имени самого бота, без Business.
- Не больше одного сообщения в сутки на чат (настройка broadcast_max_per_day), «стоп» отключает рассылки навсегда.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter)

from bot.services import grants
from bot.services import perms as perms_svc
from bot.timeutil import from_db, now_utc, to_db

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=30)  # сколько после назначенного времени ещё пытаемся отправить
BUSINESS_WINDOW = timedelta(hours=24)  # окно ответа в чатах Business (BusinessBotRights.can_reply)
MAX_SCHEDULES = 20
MAX_TEXT = 1000
MAX_TOPIC = 300
DEFAULT_TZ = "Asia/Tashkent"
DEFAULT_MAX_PER_DAY = 1
TERMINAL_PREFIXES = ("sent", "skipped", "manual_needed", "blocked", "failed", "missed", "stopped")

_DAY_NAMES = {
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7,
    "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 7,
}


class BroadcastError(ValueError):
    pass


def parse_days(raw: str) -> str:
    """daily | weekdays | weekend | 1-5 | mon,wed,fri | пн,ср -> строка ISO-дней недели «135»."""
    raw = (raw or "").strip().lower()
    if raw in ("daily", "every", "ежедневно", "каждый", "har"):
        return "1234567"
    if raw in ("weekdays", "будни", "будние"):
        return "12345"
    if raw in ("weekend", "выходные"):
        return "67"
    days: set[int] = set()
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        m = re.fullmatch(r"([1-7])-([1-7])", part)
        if m and int(m.group(1)) <= int(m.group(2)):
            days.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part in _DAY_NAMES:
            days.add(_DAY_NAMES[part])
        elif part.isdigit() and 1 <= int(part) <= 7:
            days.add(int(part))
        else:
            raise BroadcastError("Дни: daily, weekdays, weekend, 1-5 или пн,ср,пт (1 — понедельник).")
    if not days:
        raise BroadcastError("Не указаны дни недели.")
    return "".join(str(d) for d in sorted(days))


def parse_time(raw: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (raw or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise BroadcastError("Время в формате ЧЧ:ММ, например 10:30.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def check_tz(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise BroadcastError("Неизвестный часовой пояс.") from None
    return name


async def create_schedule(db: Any, owner: dict[str, Any], chat_id: int, send_time: str, days: str, mode: str,
                          text: str, topic: str, tz: str | None = None, channel: str | None = None) -> int:
    if mode not in ("text", "ai"):
        raise BroadcastError("Режим: text (фиксированный текст) или ai (тема для ИИ).")
    text, topic = (text or "").strip(), (topic or "").strip()
    if mode == "text" and not text:
        raise BroadcastError("Нужен текст сообщения.")
    if mode == "ai" and not topic:
        raise BroadcastError("Нужна тема для ИИ, например «спроси, как дела».")
    if len(text) > MAX_TEXT or len(topic) > MAX_TOPIC:
        raise BroadcastError(f"Текст до {MAX_TEXT} символов, тема до {MAX_TOPIC}.")
    if await db.optout_has(owner["id"], chat_id):
        raise BroadcastError("Этот человек написал «стоп»: рассылки ему отключены. Он может подписаться снова по ссылке бота.")
    if len(await db.sched_list(owner["id"])) >= MAX_SCHEDULES:
        raise BroadcastError(f"Не больше {MAX_SCHEDULES} рассылок.")
    if channel is None:
        channel = "bot" if await db.bsub_get(owner["id"], chat_id) else "business"
    if channel not in ("business", "bot"):
        raise BroadcastError("Канал: business или bot.")
    if channel == "bot" and not await db.bsub_get(owner["id"], chat_id):
        raise BroadcastError("Этот человек не подписан на бота. Подписка по ссылке: /bcast link")
    tz = check_tz(tz or (await db.get_user_setting(owner["id"], "tz")) or DEFAULT_TZ)
    return await db.sched_create(owner["id"], chat_id, parse_time(send_time), days, tz, mode, text, topic, channel,
                                 to_db(now_utc()))


async def known_chats(db: Any, owner_id: int) -> list[dict[str, Any]]:
    """Из чего можно выбирать получателя: Business-чаты, где клиент писал, и подписчики бота."""
    out = [
        {"chat_id": c["chat_id"], "name": c["client_name"], "channel": "business", "last_incoming_at": c["last_incoming_at"]}
        for c in await db.bchat_list(owner_id, 200) if c["last_incoming_at"] and not c["excluded"]
    ]
    seen = {c["chat_id"] for c in out}
    out += [{"chat_id": s["chat_id"], "name": s["name"], "channel": "bot", "last_incoming_at": None}
            for s in await db.bsub_list(owner_id) if s["chat_id"] not in seen]
    return out


def is_due(sch: dict[str, Any], now: datetime) -> tuple[bool, bool]:
    """(пора отправлять, окно пропущено). Отправка один раз в локальный день, только в окне GRACE после времени."""
    tz = ZoneInfo(sch["tz"])
    local = now.astimezone(tz)
    if str(local.isoweekday()) not in sch["weekdays"] or sch["last_run_date"] == local.date().isoformat():
        return False, False
    hh, mm = (int(x) for x in sch["send_time"].split(":"))
    planned = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    created = from_db(sch["created_at"])
    if local < planned or (created and created.astimezone(tz) > planned + GRACE):
        return False, False
    if local - planned <= GRACE:
        return True, False
    return False, True


def _is_terminal(status: str) -> bool:
    return status.split(":", 1)[0] in TERMINAL_PREFIXES


AI_SYSTEM = """Ты — Кимо, AI-агент {owner}. Напиши ОДНО короткое (1–3 предложения) сообщение клиенту на тему: «{topic}».
Пиши на языке клиента из переписки (по умолчанию русский), дружелюбно и без давления. Не придумывай цены, скидки, сроки.
Не выдавай себя за человека. Запреты владельца: {bans}. Тон: {tone}.
Ответ — ТОЛЬКО JSON: {{"text":"<сообщение>"}}"""


class Broadcaster:
    def __init__(self, bot: Bot, db: Any, settings: Any, llm: Any = None, clock: Callable[[], datetime] = now_utc,
                 pause: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.bot, self.db, self.settings, self.llm, self.clock, self.pause = bot, db, settings, llm, clock, pause

    async def tick(self) -> None:
        now = self.clock()
        try:
            for sch in await self.db.sched_all_active():
                due, missed = is_due(sch, now)
                if missed:
                    await self._finish(sch, now, "missed: время рассылки прошло (бот был выключен или ждал лимит)")
                elif due:
                    await self.run_one(sch, now)
                    await self.pause(1.0)  # не чаще одного сообщения в секунду
        except Exception:  # noqa: BLE001 — фоновая задача не должна падать
            log.exception("Ошибка в рассылках")

    async def _finish(self, sch: dict[str, Any], now: datetime, status: str, detail: str = "") -> str:
        local_date = now.astimezone(ZoneInfo(sch["tz"])).date().isoformat()
        terminal = _is_terminal(status)
        fields: dict[str, Any] = {"last_status": status[:200]}
        if terminal:
            fields.update(last_run=to_db(now), last_run_date=local_date)
            await self.db.blog_add(sch["id"], sch["owner_id"], sch["chat_id"], status.split(":", 1)[0], detail or status, to_db(now))
        await self.db.sched_update(sch["owner_id"], sch["id"], **fields)
        return status

    async def run_one(self, sch: dict[str, Any], now: datetime | None = None) -> str:
        now = now or self.clock()
        owner = await self.db.get_user(sch["owner_id"])
        if owner is None or not await grants.has_access(self.db, owner, self.settings, now) \
                or not await perms_svc.can(self.db, owner, "broadcasts", self.settings.admin_id):
            return await self._finish(sch, now, "blocked: нет доступа или права broadcasts")
        owner_id, chat_id = owner["id"], sch["chat_id"]
        if await self.db.optout_has(owner_id, chat_id):
            await self.db.sched_update(owner_id, sch["id"], active=0)
            return await self._finish(sch, now, "stopped: клиент написал «стоп»")

        max_per_day = int((await self.db.get_user_setting(owner_id, "broadcast_max_per_day")) or DEFAULT_MAX_PER_DAY)
        tz = ZoneInfo(sch["tz"])
        day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if await self.db.blog_sent_since(owner_id, chat_id, to_db(day_start)) >= max_per_day:
            return await self._finish(sch, now, "skipped: лимит сообщений в сутки на чат")

        conn_id: str | None = None
        manual_reason = ""
        if sch["channel"] == "bot":
            if await self.db.bsub_get(owner_id, chat_id) is None:
                return await self._finish(sch, now, "skipped: человек не подписан на бота")
        else:
            chat = await self.db.bchat_get(owner_id, chat_id)
            if chat is None or not chat["last_incoming_at"]:
                return await self._finish(sch, now, "skipped: человек ещё не писал владельцу")
            if chat["excluded"]:
                return await self._finish(sch, now, "skipped: чат исключён")
            conns = [c for c in await self.db.bconn_list(owner_id) if c["is_enabled"] and c["can_reply"]]
            last_in = from_db(chat["last_incoming_at"])
            if not conns:
                manual_reason = "нет Business-подключения с правом отвечать"
            elif last_in is None or now - last_in > BUSINESS_WINDOW:
                manual_reason = "Telegram разрешает писать только в течение 24 часов после сообщения клиента"
            else:
                conn_id = conns[0]["connection_id"]

        text = await self._text(sch, owner, now)
        if text is None:
            still_in_window, _ = is_due(sch, now + timedelta(minutes=1))
            return await self._finish(sch, now, "retry: ИИ недоступен, повторю" if still_in_window else "failed: ИИ недоступен")
        if manual_reason:
            await self._notify_manual(owner, sch, text, manual_reason)
            return await self._finish(sch, now, f"manual_needed: {manual_reason}")

        try:
            sent = await self.bot.send_message(chat_id, text, business_connection_id=conn_id, parse_mode=None)
        except TelegramRetryAfter as exc:
            return await self._finish(sch, now, f"retry: Telegram просит подождать {exc.retry_after} с")
        except TelegramNetworkError:
            return await self._finish(sch, now, "retry: сеть Telegram недоступна")
        except TelegramForbiddenError as exc:
            if sch["channel"] == "bot":
                await self.db.bsub_remove(owner_id, chat_id)
                await self.db.sched_update(owner_id, sch["id"], active=0)
                return await self._finish(sch, now, "failed: человек заблокировал бота, подписка снята", type(exc).__name__)
            await self._notify_manual(owner, sch, text, f"Telegram отклонил отправку ({type(exc).__name__})")
            return await self._finish(sch, now, f"manual_needed: Telegram отклонил отправку ({type(exc).__name__})")
        except TelegramAPIError as exc:
            reason = f"Telegram отклонил отправку ({type(exc).__name__}: {str(exc)[:80]})"
            await self._notify_manual(owner, sch, text, reason)
            return await self._finish(sch, now, f"manual_needed: {reason}")
        if conn_id:
            await self.db.bmsg_add(owner_id, chat_id, "assistant", text, sent.message_id, to_db(now))
        return await self._finish(sch, now, "sent", text[:100])

    async def _text(self, sch: dict[str, Any], owner: dict[str, Any], now: datetime) -> str | None:
        if sch["mode"] == "text":
            return sch["text"]
        if self.llm is None:
            return None
        rules = await self.db.brules_get(owner["id"]) or {}
        name = (rules.get("owner_name") or owner.get("first_name") or "владельца").strip()
        system = AI_SYSTEM.format(owner=name, topic=sch["topic"], bans=rules.get("prohibitions") or "нет",
                                  tone=rules.get("tone") or "дружелюбный")
        history = await self.db.bmsg_recent(owner["id"], sch["chat_id"], 6)
        messages = [{"role": "system", "content": system}] + [
            {"role": "assistant" if m["role"] == "assistant" else "user", "content": m["text"][:300]} for m in history
        ] + [{"role": "user", "content": "Напиши сообщение по теме."}]
        try:
            from bot.ai.queue import P_BACKGROUND

            llm = self.llm.bound(P_BACKGROUND) if hasattr(self.llm, "bound") else self.llm
            raw = await llm.chat_json(messages, max_tokens=300)
        except Exception as exc:  # noqa: BLE001 — любая ошибка ИИ: повторим позже
            log.info("Рассылка %s: ИИ недоступен (%s)", sch["id"], type(exc).__name__)
            return None
        try:
            data = json.loads(raw)
            text = str(data.get("text") or "").strip()
        except (ValueError, AttributeError):
            return None
        return text[:MAX_TEXT] or None

    async def _notify_manual(self, owner: dict[str, Any], sch: dict[str, Any], text: str, reason: str) -> None:
        try:
            await self.bot.send_message(
                owner["tg_id"],
                f"📨 Рассылка #{sch['id']} не отправлена автоматически: {reason}.\n"
                f"Отправьте вручную в чат с клиентом (ID {sch['chat_id']}):\n\n{text}",
                parse_mode=None,
            )
        except TelegramAPIError as exc:
            log.warning("Не удалось уведомить владельца о ручной рассылке: %s", type(exc).__name__)
