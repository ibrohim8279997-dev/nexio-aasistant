"""Правила исходящих звонков: не чаще раза в 10 минут, ночью (23:00-07:00 по Ташкенту) только срочные."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MIN_INTERVAL = timedelta(minutes=10)
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 7


@dataclass(frozen=True)
class CallDecision:
    ok: bool
    reason: str = ""


def is_night(now_local: datetime) -> bool:
    return now_local.hour >= NIGHT_START_HOUR or now_local.hour < NIGHT_END_HOUR


def check_call(now_local: datetime, last_attempt: datetime | None, urgent: bool) -> CallDecision:
    """now_local и last_attempt — время в одной зоне (Asia/Tashkent). Срочность отменяет только ночное ограничение."""
    if last_attempt is not None and now_local - last_attempt < MIN_INTERVAL:
        wait = int((MIN_INTERVAL - (now_local - last_attempt)).total_seconds() // 60) + 1
        return CallDecision(False, f"звонок уже был меньше 10 минут назад (следующий через ~{wait} мин)")
    if is_night(now_local) and not urgent:
        return CallDecision(False, "ночное время (с 23:00 до 07:00) и задание не срочное")
    return CallDecision(True)
