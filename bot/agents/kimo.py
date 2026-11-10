"""Кимо как участник шины: отвечает другим агентам про задачи и напоминания владельца.

Расходы другим агентам не раскрываются: действия с расходами из шины не выполняются.
"""
from __future__ import annotations

import logging
from typing import Any

from bot.agents.bus import BusError, NeedsClarification, plain_text
from bot.agents.context import AgentContext
from bot.ai.actions import AddExpense, Clarify, ShowStats, build_messages, parse_actions
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
    "{context}"
)


def context_note(payload: dict[str, Any]) -> str:
    """Контекст запроса из payload: кто спрашивает, о чём, что нужно (см. Bus.prepare_payload)."""
    context = payload.get("context") or {}
    parts = [
        f"{label}: {str(context[key])[:300]}"
        for key, label in (("from_name", "спрашивает"), ("owner_request", "исходный запрос владельца"),
                           ("about", "о чём"), ("need", "что нужно"))
        if context.get(key)
    ]
    return ("\nКонтекст запроса — " + "; ".join(parts) + ".") if parts else ""


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
        identity = await self.ctx.identity() + "\n" + BUS_NOTE.format(
            sender=task["from_agent"], context=context_note(payload)
        )
        open_tasks = await open_tasks_for_prompt(self.db, owner["id"])
        raw = await self.llm.chat_json(build_messages(text, context_now_text(), open_tasks, identity))
        parsed = parse_actions(raw, now_local().date())
        for action in parsed:
            if isinstance(action, Clarify):
                # не молча done с вопросом внутри: failed с причиной, вопрос уходит владельцу
                raise NeedsClarification(f"Кимо не понял задание, нужно уточнение: {action.question}", action.question)
        actions = [a for a in parsed if not is_private_action(a)]
        if not actions:
            reason = "расходы другим агентам не передаются" if parsed else "не удалось понять запрос"
            raise BusError(f"Кимо не смог выполнить задание: {reason}")
        deps = assistant.AssistantDeps(db=self.db, user=owner, rates=self.rates, identity=identity)
        answers = await assistant.execute(actions, deps)
        return plain_text("\n\n".join(a.text for a in answers))
