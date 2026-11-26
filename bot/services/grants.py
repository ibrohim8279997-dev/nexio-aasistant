"""Доступ пользователей к Кимо: /grant, /revoke, автоотзыв по сроку, уведомления.

Админ (ADMIN_ID) имеет доступ всегда. Остальные — по активной записи в access_grants или оплаченной подписке (users.sub_until).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from bot.services import subscription as sub
from bot.timeutil import from_db, now_utc, to_db

log = logging.getLogger(__name__)

MAX_GRANT_DAYS = 3650
REMIND_BEFORE = timedelta(days=1)


class GrantError(ValueError):
    pass


async def active_until(db: Any, user: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """До какого момента у пользователя открыт доступ по /grant (None — нет активного доступа)."""
    now = now or now_utc()
    row = await db.grant_active(user["id"])
    until = from_db(row["expires_at"]) if row else None
    return until if until and until > now else None


async def has_access(db: Any, user: dict[str, Any] | None, settings: Any, now: datetime | None = None) -> bool:
    """Админ, активная подписка или активный доступ /grant."""
    if user is None:
        return False
    now = now or now_utc()
    if sub.has_access(user["tg_id"], user.get("sub_until"), settings.admin_id, now):
        return True
    return await active_until(db, user, now) is not None


async def grant(db: Any, user: dict[str, Any], days: int, granted_by: int, now: datetime | None = None) -> datetime:
    """Выдать или продлить доступ на days дней (от текущего окончания, если оно ещё не наступило)."""
    if not 1 <= days <= MAX_GRANT_DAYS:
        raise GrantError(f"Дней: от 1 до {MAX_GRANT_DAYS}.")
    now = now or now_utc()
    current = await active_until(db, user, now)
    expires = (current or now) + timedelta(days=days)
    await db.grant_upsert(user["id"], granted_by, to_db(expires), to_db(now))
    return expires


async def revoke(db: Any, user: dict[str, Any], now: datetime | None = None) -> bool:
    """Отозвать доступ (и оплаченную подписку тоже). True, если что-то было отозвано."""
    now = now or now_utc()
    changed = False
    row = await db.grant_active(user["id"])
    if row:
        changed = await db.grant_close(row["id"], "revoked", to_db(now))
    if user.get("sub_until") and sub.is_active(user["sub_until"], now):
        await db.set_sub_until(user["id"], None)
        changed = True
    return changed


async def expire_due(db: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """Закрыть истёкшие доступы. Возвращает закрытые записи (с tg_id) для уведомлений."""
    now = now or now_utc()
    closed = []
    for row in await db.grants_expired(to_db(now)):
        if await db.grant_close(row["id"], "expired", to_db(now)):
            closed.append(row)
    return closed


async def due_reminders(db: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or now_utc()
    return await db.grants_to_remind(to_db(now), to_db(now + REMIND_BEFORE))


def fmt(expires_at: str | datetime | None) -> str:
    return sub.fmt_until(to_db(expires_at) if isinstance(expires_at, datetime) else expires_at)


def no_access_text(user_tg_id: int) -> str:
    return (
        "🔒 Это личный бот, доступ выдаёт владелец.\n"
        f"Отправьте владельцу ваш ID: <code>{user_tg_id}</code> — он откроет доступ командой /grant."
    )
