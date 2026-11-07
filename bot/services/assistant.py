"""Выполнение проверенных действий от модели."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from bot.ai.actions import AddExpense, AddTask, CompleteTask, ListTasks, Reply, ShowStats
from bot.db import Database
from bot.keyboards import task_list_kb
from bot.services import tasks as tasks_service
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


async def execute(actions: list[Any], deps: AssistantDeps) -> list[Answer]:
    answers: list[Answer] = []
    for action in actions:
        try:
            answers.append(await _execute_one(action, deps))
        except tasks_service.TaskInputError as exc:
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

    if isinstance(action, (AddExpense, ShowStats)):
        return Answer("Этот раздел ещё не подключён.")

    if isinstance(action, Reply):
        return Answer(escape(action.text))

    log.warning("Неизвестное действие: %s", type(action).__name__)
    return Answer("🤔 Не понял запрос.")
