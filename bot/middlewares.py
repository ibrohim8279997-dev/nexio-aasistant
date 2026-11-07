"""Middleware: антиспам."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

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
