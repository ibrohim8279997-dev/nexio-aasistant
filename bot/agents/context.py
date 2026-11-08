"""Контекст агента в процессе: конфиг, реестр, системная БД, шина."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.agents.registry import AgentConfig, Registry
from bot.db import Database


@dataclass
class AgentContext:
    agent: AgentConfig
    registry: Registry
    system_db: Database  # только для системных сервисов (реестр, шина); данные — через ScopedDB
    bus: Any = None  # bot.agents.bus.Bus, подключается в run_agent

    async def identity(self) -> str:
        return await self.registry.identity_prompt(self.agent)
