"""Выполнение проверенных действий от модели."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from bot.ai.actions import (
    AgentData,
    AgentLogs,
    AgentsReport,
    AgentTables,
    AddExpense,
    AddTask,
    Clarify,
    CompleteTask,
    CreateBroadcast,
    OpenApp,
    PauseBusiness,
    CreateAgent,
    Delegate,
    DiagnoseAgent,
    ListReminders,
    ListTasks,
    ManageAgent,
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

ADMIN_ACTIONS = (ManageAgent, AgentsReport, AgentLogs, AgentTables, AgentData, DiagnoseAgent)


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
    offer_restart: Any = None  # async (AgentConfig, text) -> str: предложить перезапуск с подтверждением
    admin: Any = None  # AdminActions
    miniapp_url: str = ""  # публичный адрес Mini App для open_app
    perms: frozenset[str] | None = None  # None — без ограничений (владелец, шина); иначе действующие права пользователя
    consulted: list[str] = field(default_factory=list)  # какие агенты отвечали (для audit_log голосовых обращений)


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


NO_RIGHTS = Answer("🔒 Эта функция вам недоступна. Её может включить владелец.")
# какое право нужно для действия
_NEEDS: tuple[tuple[Any, str], ...] = (
    (AddTask, "tasks"), (CompleteTask, "tasks"), (ListTasks, "tasks"), (ListReminders, "reminders"),
    (AddExpense, "finance"), (CreateBroadcast, "broadcasts"), (PauseBusiness, "business"),
    (Delegate, "agents_manage"), (CreateAgent, "agents_manage"),
    (ManageAgent, "agents_manage"), (AgentsReport, "agents_manage"), (AgentLogs, "agents_manage"),
    (AgentTables, "agents_manage"), (AgentData, "agents_manage"), (DiagnoseAgent, "agents_manage"),
)


def needed_perm(action: Any) -> str | None:
    if isinstance(action, ShowStats):
        return "finance" if action.topic == "expenses" else "tasks"
    for cls, perm in _NEEDS:
        if isinstance(action, cls):
            return perm
    return None


async def _execute_one(action: Any, deps: AssistantDeps) -> Answer:
    db, user_id = deps.db, deps.user["id"]
    need = needed_perm(action)
    if deps.perms is not None and need and need not in deps.perms:
        return NO_RIGHTS

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
            db, deps.rates, user_id, action.amount, action.currency, action.category, action.note, action.kind
        )
        return Answer(
            expenses_service.created_text(
                action.amount, action.currency, expenses_service.clean_category(action.category), action.note, info,
                action.kind,
            )
        )

    if isinstance(action, ShowStats) and action.topic == "expenses":
        return Answer(await expenses_service.summary_text(db, deps.rates, user_id, action.period))

    if isinstance(action, ShowStats):
        return Answer(await tracker.tracker_text(db, user_id), stats_menu())

    if isinstance(action, Reply):
        return Answer(escape(action.text))

    if isinstance(action, CreateBroadcast):
        return await _create_broadcast(action, deps)

    if isinstance(action, PauseBusiness):
        return await _pause_business(action, deps)

    if isinstance(action, OpenApp):
        return _open_app(action, deps)

    if isinstance(action, ListReminders):
        return Answer(await _reminders_text(db, user_id))

    if isinstance(action, Clarify):
        return Answer(f"❓ {escape(action.question)}")

    if isinstance(action, Delegate):
        return await _delegate(action, deps)

    if isinstance(action, CreateAgent):
        return await _create_agent(action, deps)

    if isinstance(action, ADMIN_ACTIONS):
        if deps.admin is None:
            return Answer("Управление агентами здесь недоступно.")
        text = await deps.admin.run(action, owner=deps.is_owner, chat_id=deps.chat_id)
        return Answer(text or "🤔 Не понял запрос.")

    log.warning("Неизвестное действие: %s", type(action).__name__)
    return Answer("🤔 Не понял запрос.")


async def _resolve_chat(db: Any, owner_id: int, ref: str) -> tuple[int | None, str | None]:
    """ID или имя клиента -> chat_id. Второй элемент — сообщение, если чат не найден или имя неоднозначно."""
    ref = ref.strip()
    chats = await db.bchat_list(owner_id, 200)
    if ref.lstrip("-").isdigit():
        return int(ref), None
    needle = ref.casefold().lstrip("@")
    found = [c for c in chats if needle in (c["client_name"] or "").casefold() or needle == (c["username"] or "").casefold()]
    if len(found) == 1:
        return found[0]["chat_id"], None
    if not found:
        return None, "❓ Не нашёл такого клиента среди ваших чатов. Список: /bchats"
    names = ", ".join(f"{c['client_name'] or c['chat_id']} ({c['chat_id']})" for c in found[:5])
    return None, f"❓ Уточните, какой клиент: {escape(names)}"


async def _create_broadcast(action: CreateBroadcast, deps: AssistantDeps) -> Answer:
    from bot.services import broadcast as bcast

    chat_id, problem = await _resolve_chat(deps.db, deps.user["id"], action.chat)
    if chat_id is None:
        return Answer(problem or "❓ Какому клиенту писать?")
    try:
        sid = await bcast.create_schedule(
            deps.db, deps.user, chat_id, action.time, bcast.parse_days(action.days), action.mode,
            action.text if action.mode == "text" else "", action.topic if action.mode == "ai" else "",
        )
    except bcast.BroadcastError as exc:
        return Answer(f"⚠️ {escape(str(exc))}")
    what = action.text if action.mode == "text" else f"ИИ: {action.topic}"
    return Answer(f"✅ Рассылка #{sid}: чат <code>{chat_id}</code>, {bcast.parse_time(action.time)}, {escape(what[:80])}\n"
                  "Список и изменение: /bcast или Mini App.")


async def _pause_business(action: PauseBusiness, deps: AssistantDeps) -> Answer:
    owner = deps.user["id"]
    if action.chat:
        chat_id, problem = await _resolve_chat(deps.db, owner, action.chat)
        if chat_id is None:
            return Answer(problem or "❓ В каком чате?")
        extra: dict[str, Any] = {} if not action.enabled else {"paused_until": None}
        if not await deps.db.bchat_set(owner, chat_id, excluded=int(not action.enabled), **extra):
            return Answer("❓ Такого чата нет среди чатов клиентов: /bchats")
        return Answer(f"{'▶️ Кимо отвечает' if action.enabled else '⏸ Кимо молчит'} в чате <code>{chat_id}</code>.")
    if not await deps.db.bconn_set_ai(owner, action.enabled):
        return Answer("Business ещё не подключён: Telegram → Настройки → Telegram Business → Чат-боты.")
    return Answer("▶️ Кимо снова отвечает клиентам." if action.enabled else "⏸ Кимо молчит во всех Business-чатах. Вернуть: «включи ответы клиентам».")


def _open_app(action: OpenApp, deps: AssistantDeps) -> Answer:
    from aiogram.types import InlineKeyboardButton, WebAppInfo

    if not deps.miniapp_url.startswith("https://"):
        return Answer("Mini App ещё не настроен: нужен публичный HTTPS-адрес (MINIAPP_URL, см. README).")
    url = deps.miniapp_url if action.section == "home" else f"{deps.miniapp_url}/?s={action.section}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Открыть", web_app=WebAppInfo(url=url))]])
    return Answer(f"Открываю раздел «{escape(action.section)}»:", kb)


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
            return Answer(escape(await deps.offer_restart(target, text)))
        return Answer(text + f"\nЗапустить агента: /agenton {target.name}")
    deps.consulted.append(target.name)
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
