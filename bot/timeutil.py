"""Время: локальная зона Asia/Tashkent, в БД храним UTC."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Tashkent")
DB_FMT = "%Y-%m-%d %H:%M:%S"

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return datetime.now(TZ)


def to_db(dt: datetime) -> str:
    """Aware datetime -> строка UTC для SQLite."""
    if dt.tzinfo is None:
        raise ValueError("Нужен datetime с часовым поясом")
    return dt.astimezone(timezone.utc).strftime(DB_FMT)


def from_db(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, DB_FMT).replace(tzinfo=timezone.utc)


def local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=TZ)


def day_bounds(day: date) -> tuple[str, str]:
    """Границы локального дня в UTC-строках [start, end)."""
    start = local_midnight(day)
    return to_db(start), to_db(start + timedelta(days=1))


def period_start(period: str, today: date) -> date:
    """day — сегодня, week — последние 7 дней, month — последние 30 дней."""
    days = {"day": 1, "week": 7, "month": 30}[period]
    return today - timedelta(days=days - 1)


def period_bounds(period: str, today: date) -> tuple[str, str]:
    start = period_start(period, today)
    return to_db(local_midnight(start)), to_db(local_midnight(today + timedelta(days=1)))


def fmt_local(dt: datetime | None, with_date: bool = True) -> str:
    if dt is None:
        return ""
    local = dt.astimezone(TZ)
    return local.strftime("%d.%m %H:%M" if with_date else "%H:%M")


def context_now_text() -> str:
    """Строка с текущим временем для промпта модели."""
    now = now_local()
    return f"{now.strftime('%Y-%m-%d %H:%M')} ({WEEKDAYS[now.weekday()]}), часовой пояс Asia/Tashkent"
