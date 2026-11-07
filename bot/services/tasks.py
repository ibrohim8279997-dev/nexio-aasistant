"""Задачи: сроки, проверка ввода, вывод списков."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from typing import Any

from bot.db import Database
from bot.timeutil import TZ, day_bounds, from_db, local_midnight, now_local, to_db

MAX_TASK_LEN = 500
MAX_LIST = 30
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class TaskInputError(ValueError):
    """Некорректный ввод задачи (сообщение можно показать пользователю)."""


def clean_task_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise TaskInputError("Текст задачи пустой.")
    if len(text) > MAX_TASK_LEN:
        raise TaskInputError(f"Слишком длинная задача (максимум {MAX_TASK_LEN} символов).")
    return text


def resolve_due(day: date | None, at: time | None, now: datetime) -> tuple[datetime, bool]:
    """Дата/время -> (локальный срок, указано ли время). Без даты — сегодня; прошедшее время без даты — завтра."""
    today = now.date()
    if day is None and at is not None:
        due = datetime.combine(today, at, tzinfo=TZ)
        if due <= now:
            due += timedelta(days=1)
        return due, True
    target = day or today
    if at is None:
        return local_midnight(target), False
    return datetime.combine(target, at, tzinfo=TZ), True


def check_due_range(due: datetime, now: datetime) -> None:
    if due.date() < now.date() - timedelta(days=1):
        raise TaskInputError("Дата в прошлом.")
    if due.date() > now.date() + timedelta(days=5 * 366):
        raise TaskInputError("Дата слишком далеко в будущем.")


async def create_task(db: Database, user_id: int, text: str, due: datetime, has_time: bool) -> int:
    text = clean_task_text(text)
    check_due_range(due, now_local())
    return await db.add_task(user_id, text, to_db(due), has_time, to_db(now_local()))


def human_day(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    return f"{day.strftime('%d.%m')} ({WEEKDAYS_SHORT[day.weekday()]})"


def human_due(due: datetime, has_time: bool, today: date | None = None) -> str:
    local = due.astimezone(TZ)
    day = human_day(local.date(), today or now_local().date())
    return f"{day}, {local.strftime('%H:%M')}" if has_time else day


def created_text(text: str, due: datetime, has_time: bool) -> str:
    reminder = " — напомню" if has_time else ""
    return f"📌 Задача добавлена: <b>{escape(text)}</b>\n🗓 {human_due(due, has_time)}{reminder}"


def day_for(name: str, now: datetime | None = None) -> date:
    today = (now or now_local()).date()
    return today + timedelta(days=1) if name == "tomorrow" else today


@dataclass
class TaskListView:
    text: str
    buttons: list[tuple[int, int]]  # (номер, id) для невыполненных


def _task_line(num: int, task: dict[str, Any], day_start: datetime) -> str:
    text = escape(task["text"])
    due = from_db(task["due_at"])
    if task["done_at"]:
        return f"{num}. ✅ <s>{text}</s>"
    if due and due < day_start:
        when = due.astimezone(TZ).strftime("%d.%m %H:%M" if task["has_time"] else "%d.%m")
        return f"{num}. ⚠️ <i>{when}</i> — {text}"
    if due and task["has_time"]:
        return f"{num}. ⏰ <b>{due.astimezone(TZ).strftime('%H:%M')}</b> — {text}"
    return f"{num}. • {text}"


async def render_day(db: Database, user_id: int, day_name: str) -> TaskListView:
    now = now_local()
    day = day_for(day_name, now)
    start, end = day_bounds(day)
    tasks = await db.tasks_between(user_id, start, end, overdue_before=start if day_name == "today" else None)
    tasks = tasks[:MAX_LIST]
    title = ("📅 <b>Сегодня</b>" if day_name == "today" else "🗓 <b>Завтра</b>") + f", {day.strftime('%d.%m')}"
    if not tasks:
        return TaskListView(f"{title}\n\nЗадач нет. Напишите или скажите, что нужно сделать.", [])

    day_start = local_midnight(day)
    lines = [_task_line(i, t, day_start) for i, t in enumerate(tasks, start=1)]
    done = sum(1 for t in tasks if t["done_at"])
    buttons = [(i, t["id"]) for i, t in enumerate(tasks, start=1) if not t["done_at"]]
    footer = f"\nВыполнено: {done} из {len(tasks)}"
    if buttons:
        footer += "\n\n✅ — выполнено · ⏰ — перенести · 🗑 — удалить"
    return TaskListView(f"{title}\n\n" + "\n".join(lines) + "\n" + footer, buttons)


async def open_tasks_for_prompt(db: Database, user_id: int) -> list[str]:
    lines = []
    for task in await db.open_tasks(user_id, limit=30):
        due = from_db(task["due_at"])
        when = due.astimezone(TZ).strftime("%Y-%m-%d %H:%M" if task["has_time"] else "%Y-%m-%d") if due else "без срока"
        lines.append(f"#{task['id']} [{when}] {task['text']}")
    return lines


# ---------- локальный разбор ввода (без ИИ) ----------

_DAY_WORDS = {
    "сегодня": 0, "bugun": 0, "завтра": 1, "ertaga": 1, "послезавтра": 2, "indinga": 2,
}
_DAY_RE = re.compile(r"(?<!\w)(" + "|".join(_DAY_WORDS) + r")(?!\w)", re.IGNORECASE)
_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?(?![\d.])")
_TIME_RE = re.compile(r"(?:(?<!\w)(?:в|soat)\s+)?(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])", re.IGNORECASE)


@dataclass
class ParsedTask:
    text: str
    day: date | None
    at: time | None


def parse_task_input(raw: str, today: date) -> ParsedTask:
    """«завтра 15:00 позвонить маме», «12.11 09:30 врач», «купить хлеб» -> ParsedTask."""
    text = raw
    day: date | None = None
    at: time | None = None

    if m := _DAY_RE.search(text):
        day = today + timedelta(days=_DAY_WORDS[m.group(1).lower()])
        text = text[: m.start()] + " " + text[m.end() :]
    elif m := _DATE_RE.search(text):
        d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = (2000 + int(y) if len(y) == 2 else int(y)) if y else today.year
        try:
            day = date(year, mth, d)
        except ValueError:
            raise TaskInputError("Неверная дата. Формат: ДД.ММ или ДД.ММ.ГГГГ") from None
        if not y and day < today:
            day = date(year + 1, mth, d)
        text = text[: m.start()] + " " + text[m.end() :]

    if m := _TIME_RE.search(text):
        at = time(int(m.group(1)), int(m.group(2)))
        text = text[: m.start()] + " " + text[m.end() :]

    return ParsedTask(text=re.sub(r"\s+", " ", text).strip(" ,.-—"), day=day, at=at)


def move_one_hour(due: datetime, has_time: bool, now: datetime) -> datetime:
    base = due if has_time and due > now else now
    return (base + timedelta(hours=1)).replace(second=0, microsecond=0)


def move_to_tomorrow(due: datetime, has_time: bool, now: datetime) -> datetime:
    tomorrow = now.date() + timedelta(days=1)
    if has_time:
        return datetime.combine(tomorrow, due.astimezone(TZ).time(), tzinfo=TZ)
    return local_midnight(tomorrow)
