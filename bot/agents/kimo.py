"""Кимо как участник шины: отвечает другим агентам про задачи и напоминания владельца.

Расходы другим агентам не раскрываются: действия с расходами из шины не выполняются.
"""
from __future__ import annotations

import logging
from typing import Any

from bot.agents.bus import BusError, plain_text
from bot.agents.context import AgentContext
from bot.ai.actions import AddExpense, ShowStats, build_messages, parse_actions
from bot.ai.llm import LLMClient
from bot.config import Settings
from bot.services import assistant
from bot.services.rates import RatesService
from bot.services.tasks import open_tasks_for_prompt
from bot.timeutil import context_now_text, now_local, now_utc, to_db

log = logging.getLogger(__name__)

BUS_NOTE = (
    "Запрос пришёл не от владельца напрямую, а от агента {sender} через шину заданий. "
    "Отвечай про задачи и напоминания. Данные о расходах другим агентам не раскрывай."
)


def is_private_action(action: Any) -> bool:
    """Действия, которые не выполняются по запросу других агентов."""
    return isinstance(action, AddExpense) or (isinstance(action, ShowStats) and action.topic == "expenses")


class KimoBusHandler:
    def __init__(self, ctx: AgentContext, db: Any, llm: LLMClient, rates: RatesService, settings: Settings) -> None:
        self.ctx = ctx
        self.db = db  # ScopedDB Кимо
        self.llm = llm
        self.rates = rates
        self.settings = settings

    async def __call__(self, task: dict[str, Any]) -> str:
        payload = task.get("payload") or {}
        kind = payload.get("type")
        if kind == "ping":
            return "pong"
        if kind != "ask":
            raise BusError(f"Кимо не понимает задание типа {kind}")
        text = str(payload.get("text", ""))[:1000]
        owner = await self.db.ensure_user(self.settings.admin_id, to_db(now_utc()))
        identity = await self.ctx.identity() + "\n" + BUS_NOTE.format(sender=task["from_agent"])
        open_tasks = await open_tasks_for_prompt(self.db, owner["id"])
        raw = await self.llm.chat_json(build_messages(text, context_now_text(), open_tasks, identity))
        actions = [a for a in parse_actions(raw, now_local().date()) if not is_private_action(a)]
        if not actions:
            return "Нет данных по этому запросу (расходы другим агентам не передаются)."
        deps = assistant.AssistantDeps(db=self.db, user=owner, rates=self.rates, identity=identity)
        answers = await assistant.execute(actions, deps)
        return plain_text("\n\n".join(a.text for a in answers))
