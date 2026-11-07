"""Выполнение проверенных действий от модели."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from bot.ai.actions import AddExpense, AddTask, CompleteTask, ListTasks, Reply, ShowStats
from bot.db import Database
from bot.keyboards import stats_menu, task_list_kb
from bot.services import expenses as expenses_service
from bot.services import tasks as tasks_service
from bot.services import tracker
from bot.services.rates import RatesService
from bot.timeutil import now_local, to_db

log = logging.getLogger(__name__)


@dataclass
class Answer:
    text: str
    markup: InlineKeyboardMarkup | None = None


@dataclass
class AssistantDeps:
    db: Database
    user: dict[str, Any]
    rates: RatesService


async def execute(actions: list[Any], deps: AssistantDeps) -> list[Answer]:
    answers: list[Answer] = []
    for action in actions:
        try:
            answers.append(await _execute_one(action, deps))
        except (tasks_service.TaskInputError, expenses_service.ExpenseInputError) as exc:
            answers.append(Answer(f"⚠️ {exc}"))
    return answers


async def _execute_one(action: Any, deps: AssistantDeps) -> Answer:
    db, user_id = deps.db, deps.user["id"]

    if isinstance(action, AddTask):
        due, has_time = tasks_service.resolve_due(action.date, action.time, now_local())
        await tasks_service.create_task(db, user_id, action.text, due, has_time)
        return Answer(tasks_service.created_text(action.text, due, has_time))

    if isinstance(action, CompleteTask):
        task = await db.get_task(user_id, action.task_id)
        if task and await db.complete_task(user_id, action.task_id, to_db(now_local())):
            return Answer(f"✅ Выполнено: <b>{escape(task['text'])}</b>")
        return Answer("Не нашёл такую открытую задачу. Откройте список: /today")

    if isinstance(action, ListTasks):
        view = await tasks_service.render_day(db, user_id, action.day)
        return Answer(view.text, task_list_kb(view.buttons, action.day))

    if isinstance(action, AddExpense):
        _, info = await expenses_service.create_expense(
            db, deps.rates, user_id, action.amount, action.currency, action.category, action.note
        )
        return Answer(
            expenses_service.created_text(
                action.amount, action.currency, expenses_service.clean_category(action.category), action.note, info
            )
        )

    if isinstance(action, ShowStats) and action.topic == "expenses":
        return Answer(await expenses_service.summary_text(db, deps.rates, user_id, action.period))

    if isinstance(action, ShowStats):
        return Answer(await tracker.tracker_text(db, user_id), stats_menu())

    if isinstance(action, Reply):
        return Answer(escape(action.text))

    log.warning("Неизвестное действие: %s", type(action).__name__)
    return Answer("🤔 Не понял запрос.")
