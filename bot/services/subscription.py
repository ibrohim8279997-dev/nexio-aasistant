"""Подписка: проверка доступа, продление, напоминания, payload счетов."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bot.config import Settings
from bot.db import Database
from bot.timeutil import TZ, from_db, now_utc, to_db

REMIND_DAYS_BEFORE = 3
PAYLOAD_PREFIX = "sub"


def is_active(sub_until: str | None, now: datetime) -> bool:
    until = from_db(sub_until)
    return until is not None and until > now


def has_access(tg_id: int, sub_until: str | None, admin_id: int, now: datetime | None = None) -> bool:
    return tg_id == admin_id or is_active(sub_until, now or now_utc())


def extend(sub_until: str | None, now: datetime, days: int) -> datetime:
    """Продление: от текущего окончания, если подписка ещё активна, иначе от сейчас."""
    if days <= 0:
        raise ValueError("days должен быть больше нуля")
    current = from_db(sub_until)
    base = current if current and current > now else now
    return base + timedelta(days=days)


def days_left(sub_until: str | None, now: datetime) -> int:
    until = from_db(sub_until)
    if until is None or until <= now:
        return 0
    return math.ceil((until - now).total_seconds() / 86400)


def needs_reminder(sub_until: str | None, now: datetime, days_before: int = REMIND_DAYS_BEFORE) -> bool:
    until = from_db(sub_until)
    return until is not None and now < until <= now + timedelta(days=days_before)


def fmt_until(sub_until: str | None) -> str:
    until = from_db(sub_until)
    return until.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if until else "—"


@dataclass(frozen=True)
class Payload:
    method: str  # stars | click
    tg_id: int
    days: int

    def pack(self) -> str:
        return f"{PAYLOAD_PREFIX}:{self.method}:{self.tg_id}:{self.days}"

    @classmethod
    def parse(cls, raw: str) -> Payload | None:
        parts = (raw or "").split(":")
        if len(parts) != 4 or parts[0] != PAYLOAD_PREFIX or parts[1] not in ("stars", "click"):
            return None
        try:
            payload = cls(parts[1], int(parts[2]), int(parts[3]))
        except ValueError:
            return None
        return payload if payload.days > 0 else None


def expected_amount(method: str, settings: Settings) -> tuple[str, int]:
    """(валюта, сумма в минимальных единицах) для проверки платежа."""
    if method == "stars":
        return "XTR", settings.sub_price_xtr
    return "UZS", settings.click_price_uzs * 100  # тийины


def validate_payment(payload_raw: str, currency: str, total_amount: int, from_id: int, settings: Settings) -> Payload | None:
    if not settings.subscription_enabled:
        return None
    payload = Payload.parse(payload_raw)
    if payload is None or payload.tg_id != from_id or payload.days != settings.sub_days:
        return None
    if payload.method == "click" and not settings.click_enabled:
        return None
    if (currency, total_amount) != expected_amount(payload.method, settings):
        return None
    return payload


async def grant(db: Database, user: dict[str, Any], days: int) -> datetime:
    new_until = extend(user.get("sub_until"), now_utc(), days)
    await db.set_sub_until(user["id"], to_db(new_until))
    return new_until


async def revoke(db: Database, user: dict[str, Any]) -> None:
    await db.set_sub_until(user["id"], None)


def status_text(user: dict[str, Any], settings: Settings) -> str:
    now = now_utc()
    if user["tg_id"] == settings.admin_id:
        state = "включена" if settings.subscription_enabled else "выключена (SUBSCRIPTION_ENABLED=false)"
        return f"⭐ <b>Подписка</b>\nВы администратор — бот для вас бесплатный.\nПродажа подписки: {state}."
    if not settings.subscription_enabled:
        if is_active(user.get("sub_until"), now):
            return f"⭐ <b>Доступ открыт</b> до {fmt_until(user['sub_until'])}."
        return PRIVATE_TEXT
    price = f"{settings.sub_price_xtr} ⭐ за {settings.sub_days} дней"
    if is_active(user.get("sub_until"), now):
        left = days_left(user["sub_until"], now)
        return (
            f"⭐ <b>Подписка активна</b>\nДо {fmt_until(user['sub_until'])} (осталось {left} дн.)\n\n"
            f"Продлить: {price}. Новый срок добавится к текущему."
        )
    return (
        "⭐ <b>Подписка</b>\n"
        "Задачи с напоминаниями, учёт расходов в сумах и долларах, голосовые, графики и трекер.\n\n"
        f"Стоимость: <b>{price}</b>.\nОплата звёздами Telegram прямо в чате."
    )


PRIVATE_TEXT = "🔒 Это личный бот. Доступ выдаёт владелец."

PAYWALL_TEXT = (
    "🔒 Для работы с ботом нужна активная подписка.\n"
    "Оплатите звёздами Telegram — доступ откроется сразу."
)
