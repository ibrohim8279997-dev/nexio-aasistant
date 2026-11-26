"""Роли и флаги прав.

Роли: admin (ADMIN_ID, владелец системы) и user (активный доступ по /grant или подписке).
Права пользователя хранятся флагами в user_perms. Если флага нет — действует значение по умолчанию (включено).
Права ADMIN_ONLY зашиты в коде и пользователю не выдаются ни через БД, ни через API.
"""
from __future__ import annotations

from typing import Any

from bot.timeutil import now_utc, to_db

# Права по умолчанию у роли user
USER_PERMS: tuple[str, ...] = ("tasks", "finance", "reminders", "business", "broadcasts", "assistant")
# Только admin: создание агентов, токены, чужие данные, управление системой, выдача доступов
ADMIN_ONLY: tuple[str, ...] = ("agents_manage", "tokens", "other_users", "system", "grants")
ALL_PERMS = USER_PERMS + ADMIN_ONLY


def is_admin(tg_id: int | None, admin_id: int) -> bool:
    return tg_id is not None and tg_id == admin_id


def role_of(tg_id: int | None, admin_id: int) -> str:
    return "admin" if is_admin(tg_id, admin_id) else "user"


async def effective_perms(db: Any, user: dict[str, Any], admin_id: int) -> frozenset[str]:
    """Все действующие права пользователя (db — Database или ScopedDB Кимо)."""
    if is_admin(user.get("tg_id"), admin_id):
        return frozenset(ALL_PERMS)
    stored = await db.user_perm_rows(user["id"])
    return frozenset(p for p in USER_PERMS if stored.get(p, True))


async def can(db: Any, user: dict[str, Any], perm: str, admin_id: int) -> bool:
    if perm not in ALL_PERMS:
        return False  # неизвестное право не выдаётся
    if is_admin(user.get("tg_id"), admin_id):
        return True
    if perm in ADMIN_ONLY:
        return False
    return perm in await effective_perms(db, user, admin_id)


class PermError(ValueError):
    pass


async def set_perm(db: Any, user: dict[str, Any], perm: str, granted: bool) -> None:
    """Включить или выключить право пользователя. Админские права выдать нельзя."""
    if perm not in USER_PERMS:
        raise PermError(f"Право «{perm}» нельзя выдать пользователю")
    await db.set_user_perm(user["id"], perm, granted, to_db(now_utc()))
