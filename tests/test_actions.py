from datetime import date, time

import pytest

from bot.ai.actions import (
    ActionParseError,
    AddExpense,
    AddTask,
    CompleteTask,
    Reply,
    ShowStats,
    build_messages,
    parse_actions,
)

TODAY = date(2026, 11, 7)


def test_parse_task_and_expense():
    raw = """{"actions": [
        {"type": "add_task", "text": "Позвонить маме", "date": "2026-11-08", "time": "15:00"},
        {"type": "add_expense", "amount": 25000, "currency": "сум", "category": "транспорт", "note": "такси"}
    ]}"""
    task, exp = parse_actions(raw, TODAY)
    assert isinstance(task, AddTask) and task.date == date(2026, 11, 8) and task.time == time(15, 0)
    assert isinstance(exp, AddExpense) and exp.currency == "UZS" and exp.category == "Транспорт"


def test_parse_code_fence_and_single_action():
    raw = '```json\n{"type": "add_expense", "amount": 12.5, "currency": "usd", "category": "Еда"}\n```'
    (exp,) = parse_actions(raw, TODAY)
    assert exp.currency == "USD" and exp.amount == 12.5 and exp.note is None


def test_parse_text_around_json():
    raw = 'Вот ответ: {"actions":[{"type":"complete_task","task_id":12}]} готово'
    (act,) = parse_actions(raw, TODAY)
    assert isinstance(act, CompleteTask) and act.task_id == 12


def test_invalid_actions_are_dropped():
    raw = """{"actions": [
        {"type": "add_expense", "amount": -5},
        {"type": "rm_rf", "path": "/"},
        {"type": "add_task", "text": ""},
        {"type": "add_task", "text": "старое", "date": "2020-01-01"},
        {"type": "add_expense", "amount": 5000000, "currency": "USD"},
        {"type": "show_stats", "topic": "tasks", "period": "week"}
    ]}"""
    (act,) = parse_actions(raw, TODAY)
    assert isinstance(act, ShowStats) and act.topic == "tasks"


def test_all_invalid_raises():
    with pytest.raises(ActionParseError):
        parse_actions('{"actions":[{"type":"add_expense","amount":"много"}]}', TODAY)


def test_not_json_raises():
    with pytest.raises(ActionParseError):
        parse_actions("я не знаю", TODAY)


def test_too_long_task_rejected():
    raw = '{"actions":[{"type":"add_task","text":"' + "a" * 501 + '"}]}'
    with pytest.raises(ActionParseError):
        parse_actions(raw, TODAY)


def test_empty_actions_ok():
    assert parse_actions('{"actions": []}', TODAY) == []


def test_reply():
    (r,) = parse_actions('{"actions":[{"type":"reply","text":"Salom!"}]}', TODAY)
    assert isinstance(r, Reply) and r.text == "Salom!"


def test_build_messages_contains_context():
    msgs = build_messages("привет", "2026-11-07 10:00", ["#1 [2026-11-07] тест"])
    assert "2026-11-07 10:00" in msgs[0]["content"] and "#1 [2026-11-07] тест" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "привет"}
