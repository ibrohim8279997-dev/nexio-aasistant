"""Административные действия Кимо и Мимо над агентами: предложение -> «да» в личке -> выполнение -> аудит.

Роль актора (admin или creator) и область (все агенты или только созданные) проверяет AdminOps при каждом шаге.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import Bot

from bot.agents.admin_ops import AdminError, AdminOps
from bot.agents.confirm import Confirmations, ConfirmError
from bot.agents.context import AgentContext
from bot.agents.health import effective_status
from bot.agents.registry import AgentConfig

log = logging.getLogger(__name__)

OP_TO_ACTION = {
    "start": "start", "stop": "stop", "restart": "restart", "rename": "set_display_name",
    "set_role": "set_role_description", "set_prompt": "set_prompt", "set_models": "set_models",
    "grant_tables": "grant_tables", "revoke_tables": "revoke_tables",
    "grant_commands": "grant_commands", "revoke_commands": "revoke_commands",
}


class AdminActions:
    def __init__(self, ctx: AgentContext, ops: AdminOps, confirm: Confirmations, bot: Bot) -> None:
        self.ctx = ctx
        self.ops = ops
        self.confirm = confirm
        self.bot = bot

    async def find(self, query: str) -> AgentConfig | None:
        return await self.ctx.registry.find(query)

    async def manage(self, agent: str, op: str, value: Any, chat_id: int | None = None) -> str:
        """Подготовить изменение и отправить владельцу в личку на подтверждение. Возвращает текст для ответа в чат."""
        target = await self.find(agent)
        if target is None:
            return "❓ Не нашёл такого агента. Кого именно?"
        action = OP_TO_ACTION.get(op)
        if action is None:
            return "❓ Не понял, что сделать с агентом."
        try:
            proposal = await self.confirm.propose(action, target.name, {"value": value}, chat_id)
        except (AdminError, ConfirmError) as exc:
            return f"⚠️ {escape(str(exc))}"
        if not await self.confirm.send_confirmation(self.bot, proposal):
            return "⚠️ Не смог написать вам в личку. Откройте чат со мной и нажмите /start, затем повторите."
        suffix = " Это расширение прав: отдельное подтверждение." if proposal.expands else ""
        return f"📨 Отправил в личку на подтверждение (#{proposal.id}): {escape(proposal.summary.splitlines()[0])}.{suffix}"

    async def offer_restart(self, target: AgentConfig, text: str = "") -> str:
        """Предложить перезапуск агента (с подтверждением «да» в личке). Не дублирует уже ожидающее предложение."""
        for row in await self.confirm.open_pending():
            if row["target"] == target.name and row["action"] in ("restart", "start"):
                return (text + "\nПерезапуск уже предложен — ждёт вашего «да» в личке.").strip()
        try:
            proposal = await self.confirm.propose("restart", target.name)
        except (AdminError, ConfirmError) as exc:
            return (text + f"\n{escape(str(exc))}").strip()
        sent = await self.confirm.send_confirmation(self.bot, proposal)
        tail = "Предложил перезапустить агента — подтвердите «Да» в личке." if sent else "Перезапустите агента: /agenton " + target.name
        return (text + "\n" + tail).strip()

    async def offer_restart_if_dead(self, target: AgentConfig | None) -> None:
        """Для watchdog: агент offline — сразу предложить перезапуск."""
        if target is None or not target.enabled or target.name == self.ctx.agent.name:
            return
        if effective_status(target.last_seen, target.status) != "offline":
            return
        await self.offer_restart(target, "")
