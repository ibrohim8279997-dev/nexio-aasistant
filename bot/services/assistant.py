"""Выполнение проверенных действий от модели."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from bot.ai.actions import (
    AddExpense,
    AddTask,
    Clarify,
    CompleteTask,
    CreateAgent,
    Delegate,
    ListReminders,
    ListTasks,
    Reply,
    ShowStats,
)
from bot.db import Database
from bot.keyboards import agent_card_kb, stats_menu, task_list_kb
from bot.services import expenses as expenses_service
from bot.services import tasks as tasks_service
from bot.services import tracker
from bot.agents import builder_flow
from bot.agents.bus import BusError, DepthExceeded
from bot.agents.cards import card_text
from bot.agents.health import effective_status
from bot.agents.watchdog import format_offline
from bot.services.rates import RatesService
from bot.timeutil import TZ, from_db, now_local, now_utc, to_db

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
    identity: str = ""
    ctx: Any = None  # AgentContext — для передачи задач другим агентам
    chat_id: int | None = None
    reply_to: int | None = None
    is_group: bool = False
    is_owner: bool = False
    offer_restart: Any = None  # async (AgentConfig, text) -> Answer: предложить перезапуск с подтверждением


async def execute(actions: list[Any], deps: AssistantDeps) -> list[Answer]:
    answers: list[Answer] = []
    for action in actions:
        try:
            answers.append(await _execute_one(action, deps))
        except (tasks_service.TaskInputError, expenses_service.ExpenseInputError) as exc:
            answers.append(Answer(f"⚠️ {exc}"))
        except builder_flow.FlowError as exc:
            answers.append(Answer(f"⚠️ {escape(str(exc))}"))
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

    if isinstance(action, ListReminders):
        return Answer(await _reminders_text(db, user_id))

    if isinstance(action, Clarify):
        return Answer(f"❓ {escape(action.question)}")

    if isinstance(action, Delegate):
        return await _delegate(action, deps)

    if isinstance(action, CreateAgent):
        return await _create_agent(action, deps)

    log.warning("Неизвестное действие: %s", type(action).__name__)
    return Answer("🤔 Не понял запрос.")


async def _reminders_text(db: Database, user_id: int) -> str:
    now = now_utc()
    rows = await db.upcoming_reminders(user_id, to_db(now), to_db(now + timedelta(days=7)))
    if not rows:
        return "⏰ Ближайших напоминаний нет."
    today = now_local().date()
    lines = ["⏰ <b>Ближайшие напоминания</b>"]
    for task in rows:
        due = from_db(task["due_at"])
        if due:
            lines.append(f"• {tasks_service.human_due(due.astimezone(TZ), True, today)} — {escape(task['text'])}")
    return "\n".join(lines)


async def _delegate(action: Delegate, deps: AssistantDeps) -> Answer:
    ctx = deps.ctx
    if ctx is None or ctx.bus is None:
        return Answer("Передача задач другим агентам здесь недоступна.")
    target = await ctx.registry.find(action.agent, enabled_only=True)  # по display_name и по agent_id
    if target is None or target.name == ctx.agent.name:
        return Answer("❓ Не нашёл подходящего агента. Кому передать задачу?")
    # перед передачей проверяем heartbeat: offline-агенту задание не ставим, сразу говорим причину
    if effective_status(target.last_seen, target.status) == "offline":
        text = f"⚠️ {format_offline(target)}.\nЗадание не передано."
        if deps.offer_restart is not None:
            return await deps.offer_restart(target, text)
        return Answer(text + f"\nЗапустить агента: /agenton {target.name}")
    context = {
        "from": ctx.agent.name, "from_name": ctx.agent.display_name, "owner_request": action.task[:500],
        "about": action.task[:300], "need": f"выполнить задачу владельца: {action.task[:300]}",
    }
    try:
        if deps.is_group and deps.chat_id:
            # Ответит сам агент — от своего имени, реплаем на исходное сообщение (deliver_by=receiver)
            await ctx.bus.send(
                target.name,
                {"type": "ask", "text": action.task, "deliver_by": "receiver", "context": context},
                depth=1, chat_id=deps.chat_id, reply_to=deps.reply_to,
            )
            return Answer(f"👉 Задачу взял(а) <b>{escape(target.display_name)}</b>.")
        result = await ctx.bus.ask(
            target.name, action.task, depth=1, chat_id=deps.chat_id, reply_to=deps.reply_to, context=context,
            timeout=float(target.config.get("task_timeout_s", 60)) + 5.0,
        )
    except DepthExceeded as exc:
        return Answer(escape(str(exc)))
    except BusError as exc:
        return Answer(f"⚠️ {escape(target.display_name)}: {escape(str(exc))}")
    return Answer(f"<b>{escape(target.display_name)}:</b>\n{escape(result)}")


async def _create_agent(action: CreateAgent, deps: AssistantDeps) -> Answer:
    ctx = deps.ctx
    if ctx is None or ctx.bus is None or not deps.is_owner:
        return Answer("Создавать агентов может только владелец.")
    card = await builder_flow.request_card(ctx, action.description)
    card = await builder_flow.save_draft(ctx, card)
    text = card_text(card) + "\n\nЗапускаю этого агента?"
    return Answer(text, agent_card_kb(card.name, bool(card.suggested_tables or card.suggested_commands)))
