"""Middleware: антиспам, пользователь из БД, доступ по подписке."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.agents.registry import TOKEN_RE

from bot.keyboards import subscription_kb
from bot.services.subscription import PAYWALL_TEXT, PRIVATE_TEXT, has_access
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class ThrottlingMiddleware(BaseMiddleware):
    """Не больше `limit` событий в минуту на пользователя (скользящее окно)."""

    def __init__(self, limit_per_min: int, admin_id: int) -> None:
        self.limit = limit_per_min
        self.admin_id = admin_id
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._warned_at: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or self.limit <= 0 or user.id == self.admin_id:
            return await handler(event, data)

        now = time.monotonic()
        hits = self._hits[user.id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self.limit:
            if now - self._warned_at.get(user.id, 0) > 15:
                self._warned_at[user.id] = now
                await self._warn(event)
            return None
        hits.append(now)
        return await handler(event, data)

    @staticmethod
    async def _warn(event: TelegramObject) -> None:
        text = "⏳ Слишком много запросов. Подождите минуту."
        try:
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить предупреждение антиспама: %s", type(exc).__name__)


class UserMiddleware(BaseMiddleware):
    """Создаёт/находит пользователя в БД и кладёт его в data['db_user']."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        db = data.get("db")
        if user is not None and db is not None and not user.is_bot:
            data["db_user"] = await db.get_or_create_user(user.id, user.username, user.first_name, to_db(now_utc()))
        return await handler(event, data)


class AccessMiddleware(BaseMiddleware):
    """Пропускает админа, активных подписчиков и хендлеры с флагом public. Остальным — оплата."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if get_flag(data, "public"):
            return await handler(event, data)
        settings = data["settings"]
        db_user = data.get("db_user")
        if db_user and has_access(db_user["tg_id"], db_user["sub_until"], settings.admin_id):
            return await handler(event, data)
        if settings.subscription_enabled:
            text = PAYWALL_TEXT
            markup = subscription_kb(settings.sub_price_xtr, settings.click_enabled, settings.click_price_uzs)
        else:  # подписка выключена: бот личный, без кнопок оплаты
            text, markup = PRIVATE_TEXT, None
        state = data.get("state")
        if state is not None:
            await state.clear()
        if isinstance(event, Message):
            await event.answer(text, reply_markup=markup)
        elif isinstance(event, CallbackQuery):
            await event.answer()
            if event.bot is not None:
                await event.bot.send_message(event.from_user.id, text, reply_markup=markup)
        return None


GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


class GroupGuardMiddleware(BaseMiddleware):
    """Группа агентов: исполняются только сообщения владельца; токены в группе удаляются сразу;
    сообщения, адресованные другому агенту (упоминание или реплай на его бота), Кимо не перехватывает."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat = event.chat if isinstance(event, Message) else (
            event.message.chat if isinstance(event, CallbackQuery) and event.message else None
        )
        if chat is None or chat.type not in GROUP_TYPES:
            return await handler(event, data)

        if isinstance(event, Message) and TOKEN_RE.search(event.text or event.caption or ""):
            try:
                await event.delete()
            except TelegramAPIError:
                log.warning("Не удалось удалить токен из группы — дайте Кимо право удалять сообщения")
            try:
                await event.answer("🔒 Токены в группе не принимаются. Пришлите токен мне в личку.")
            except TelegramAPIError:
                pass
            return None

        user = data.get("event_from_user")
        if user is None or user.id != data["settings"].admin_id:
            if isinstance(event, CallbackQuery):
                await event.answer("Кнопки — только для владельца.")
            return None  # сообщения других участников не исполняются как команды

        if isinstance(event, Message) and await self._for_other_agent(event, data):
            return None
        return await handler(event, data)

    @staticmethod
    async def _for_other_agent(message: Message, data: dict[str, Any]) -> bool:
        ctx = data.get("agent_ctx")
        if ctx is None:
            return False
        usernames = {
            a.bot_username.lower() for a in await ctx.registry.all() if a.bot_username and a.name != ctx.agent.name
        }
        if not usernames:
            return False
        reply = message.reply_to_message
        if reply and reply.from_user and (reply.from_user.username or "").lower() in usernames:
            return True
        text = (message.text or message.caption or "").lower()
        return any(f"@{u}" in text for u in usernames)
