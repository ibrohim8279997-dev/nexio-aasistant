from datetime import date, datetime, time

import pytest

from bot.services.tasks import (
    TaskInputError,
    move_one_hour,
    move_to_tomorrow,
    parse_task_input,
    resolve_due,
)
from bot.timeutil import TZ

TODAY = date(2026, 11, 7)
NOW = datetime(2026, 11, 7, 14, 30, tzinfo=TZ)


def test_parse_tomorrow_with_time():
    p = parse_task_input("завтра в 15:00 позвонить маме", TODAY)
    assert (p.text, p.day, p.at) == ("позвонить маме", date(2026, 11, 8), time(15, 0))


def test_parse_date_without_year_rolls_over():
    p = parse_task_input("врач 05.01 09:30", TODAY)
    assert p.day == date(2027, 1, 5) and p.at == time(9, 30) and p.text == "врач"


def test_parse_uzbek_and_plain():
    assert parse_task_input("ertaga non olish", TODAY).day == date(2026, 11, 8)
    p = parse_task_input("купить хлеб", TODAY)
    assert p.day is None and p.at is None and p.text == "купить хлеб"


def test_parse_bad_date():
    with pytest.raises(TaskInputError):
        parse_task_input("31.02 что-то", TODAY)


def test_resolve_due_past_time_goes_to_tomorrow():
    due, has_time = resolve_due(None, time(9, 0), NOW)
    assert has_time and due.date() == date(2026, 11, 8)
    due, has_time = resolve_due(None, None, NOW)
    assert not has_time and due.date() == TODAY and due.hour == 0


def test_moves():
    due = datetime(2026, 11, 7, 18, 0, tzinfo=TZ)
    assert move_one_hour(due, True, NOW).hour == 19
    assert move_one_hour(due, False, NOW).hour == 15  # без времени — от текущего момента
    t = move_to_tomorrow(due, True, NOW)
    assert t.date() == date(2026, 11, 8) and t.hour == 18
