"""Трекер успеваемости: процент выполненных задач и серия дней подряд."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from bot.db import Database
from bot.timeutil import TZ, from_db, local_midnight, now_local, period_bounds, to_db


def compute_streak(done_days: Iterable[date], today: date) -> int:
    """Сколько дней подряд выполнялась хотя бы одна задача. Сегодня ещё не закончился — серия не сгорает."""
    days = set(done_days)
    current = today if today in days else today - timedelta(days=1)
    streak = 0
    while current in days:
        streak += 1
        current -= timedelta(days=1)
    return streak


def completion(rows: list[dict]) -> tuple[int, int]:
    total = len(rows)
    done = sum(1 for r in rows if r["done_at"])
    return done, total


def _bar(done: int, total: int, width: int = 10) -> str:
    if not total:
        return "░" * width
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)


@dataclass
class DailyTasks:
    days: list[date]
    done: list[int]
    total: list[int]


async def tasks_per_day(db: Database, user_id: int, days: int = 14) -> DailyTasks:
    today = now_local().date()
    start_day = today - timedelta(days=days - 1)
    rows = await db.task_rows_due_between(
        user_id, to_db(local_midnight(start_day)), to_db(local_midnight(today + timedelta(days=1)))
    )
    index = {start_day + timedelta(days=i): i for i in range(days)}
    done, total = [0] * days, [0] * days
    for r in rows:
        due = from_db(r["due_at"])
        if due is None:
            continue
        i = index.get(due.astimezone(TZ).date())
        if i is None:
            continue
        total[i] += 1
        done[i] += 1 if r["done_at"] else 0
    return DailyTasks(list(index), done, total)


async def tracker_text(db: Database, user_id: int) -> str:
    today = now_local().date()
    lines = ["🏆 <b>Трекер задач</b>", ""]
    for period, title in (("day", "Сегодня"), ("week", "7 дней"), ("month", "30 дней")):
        start, end = period_bounds(period, today)
        done, total = completion(await db.task_rows_due_between(user_id, start, end))
        pct = f"{done / total:.0%}" if total else "—"
        lines.append(f"{title}: {_bar(done, total)} {pct} ({done}/{total})")

    since = to_db(local_midnight(today - timedelta(days=365)))
    done_days = {from_db(t).astimezone(TZ).date() for t in await db.done_times_since(user_id, since)}  # type: ignore[union-attr]
    streak = compute_streak(done_days, today)
    lines.append("")
    lines.append(f"🔥 Серия: <b>{streak}</b> {_days_word(streak)} подряд")
    if streak == 0:
        lines.append("Выполните задачу сегодня, чтобы начать серию.")
    return "\n".join(lines)


def _days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"
