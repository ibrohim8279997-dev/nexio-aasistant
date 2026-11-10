"""Реестр агентов: конфиги из таблицы agents, стартовые карточки из agents/*.json, промпт с идентичностью."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.config import BASE_DIR
from bot.db import Database
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

AGENTS_DIR = BASE_DIR / "agents"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")  # формат токена BotFather


def norm_name(value: str | None) -> str:
    """Имя для сравнения: без регистра, пробелов по краям и кавычек/знаков препинания."""
    return re.sub(r"[\s\"'«»“”.,:;!?()]+", " ", (value or "").casefold()).strip()


@dataclass
class AgentConfig:
    name: str
    role: str
    system_prompt: str = ""
    models: dict[str, Any] = field(default_factory=dict)
    allowed_tables: list[str] = field(default_factory=list)
    allowed_commands: list[str] = field(default_factory=list)
    deny_rules: list[str] = field(default_factory=list)
    enabled: bool = False
    bot_username: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    display: str | None = None  # колонка display_name
    role_description: str = ""
    access_role: str = "agent"  # admin | creator | agent — права хранятся в БД, не в коде
    created_by: str | None = None
    last_seen: str | None = None
    status: str = "offline"
    status_detail: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentConfig:
        return cls(
            name=row["name"],
            role=row["role"],
            system_prompt=row.get("system_prompt") or "",
            models=row.get("models") or {},
            allowed_tables=list(row.get("allowed_tables") or []),
            allowed_commands=list(row.get("allowed_commands") or []),
            deny_rules=list(row.get("deny_rules") or []),
            enabled=bool(row.get("enabled")),
            bot_username=row.get("bot_username"),
            config=row.get("config") or {},
            display=row.get("display_name"),
            role_description=row.get("role_description") or row["role"],
            access_role=row.get("access_role") or "agent",
            created_by=row.get("created_by"),
            last_seen=row.get("last_seen"),
            status=row.get("status") or "offline",
            status_detail=row.get("status_detail") or {},
            last_error=row.get("last_error"),
        )

    @property
    def display_name(self) -> str:
        """Имя для людей. Технический идентификатор (name) не меняется никогда."""
        return str(self.display or self.config.get("display_name") or self.name)

    @property
    def kind(self) -> str:
        return str(self.config.get("kind") or "generic")

    @property
    def provider(self) -> str:
        return str(self.models.get("provider") or "groq")

    @property
    def text_model(self) -> str | None:
        return self.models.get("text") or None

    @property
    def stt_model(self) -> str | None:
        return self.models.get("stt") or None

    @property
    def search(self) -> bool:
        return bool(self.models.get("search"))

    @property
    def token_env(self) -> str:
        return str(self.config.get("token_env") or f"{self.name.upper()}_TOKEN")

    def short(self) -> str:
        return f"{self.display_name} ({self.name}): {self.role}"


def load_default_configs(directory: Path = AGENTS_DIR) -> list[dict[str, Any]]:
    cards = []
    for path in sorted(directory.glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Не прочитать %s: %s", path.name, type(exc).__name__)
            continue
        if not NAME_RE.match(str(card.get("name", ""))) or not card.get("role"):
            log.error("Некорректная карточка агента: %s", path.name)
            continue
        cards.append(card)
    return cards


class Registry:
    """Чтение реестра агентов (системный слой, не зависит от прав конкретного агента)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def seed_defaults(self) -> list[str]:
        """Добавить в БД агентов из agents/*.json, которых там ещё нет. Существующие не трогаются."""
        added = []
        for card in load_default_configs():
            if await self._db.insert_agent(card, to_db(now_utc())):
                added.append(card["name"])
        if added:
            log.info("Добавлены агенты из agents/*.json: %s", ", ".join(added))
        return added

    async def sync_from_files(self, names: list[str] | None = None) -> list[str]:
        """Перезаписать конфиг агентов из файлов, сохранив enabled и bot_username из БД."""
        synced = []
        for card in load_default_configs():
            if names and card["name"] not in names:
                continue
            current = await self._db.get_agent(card["name"])
            if current:  # живое состояние и принадлежность из файла не берём
                keep = ("enabled", "bot_username", "created_by", "last_seen", "status", "status_detail", "last_error")
                card = {**card, **{k: current[k] for k in keep}}
                card["display_name"] = current.get("display_name") or card.get("display_name")
            await self._db.insert_agent(card, to_db(now_utc()), replace=True)
            synced.append(card["name"])
        return synced

    async def get(self, name: str) -> AgentConfig | None:
        row = await self._db.get_agent(name)
        return AgentConfig.from_row(row) if row else None

    async def find(self, query: str, enabled_only: bool = False) -> AgentConfig | None:
        """Найти агента по display_name или по agent_id, регистр не важен («Мими», «мими», «assistant»)."""
        wanted = norm_name(query)
        if not wanted:
            return None
        for agent in await self.all(enabled_only=enabled_only):
            if wanted in (norm_name(agent.name), norm_name(agent.display_name)):
                return agent
        return None

    async def aliases(self, exclude: str | None = None) -> set[str]:
        """Все допустимые обращения к включённым агентам (display_name и agent_id) в нормализованном виде."""
        result: set[str] = set()
        for agent in await self.all(enabled_only=True):
            if agent.name != exclude:
                result |= {norm_name(agent.name), norm_name(agent.display_name)}
        return result

    async def all(self, enabled_only: bool = False) -> list[AgentConfig]:
        return [AgentConfig.from_row(r) for r in await self._db.list_agents(enabled_only)]

    async def others(self, name: str) -> list[AgentConfig]:
        return [a for a in await self.all(enabled_only=True) if a.name != name]

    async def identity_prompt(self, agent: AgentConfig) -> str:
        return identity_block(agent, await self.others(agent.name))


def identity_block(agent: AgentConfig, others: list[AgentConfig]) -> str:
    lines = [
        f"Тебя зовут {agent.display_name} (системное имя: {agent.name}).",
        f"Твоя роль: {agent.role}",
    ]
    if agent.system_prompt:
        lines.append(agent.system_prompt)
    if agent.deny_rules:
        lines.append("Запрещено:")
        lines.extend(f"- {rule}" for rule in agent.deny_rules)
    lines.append("Другие агенты системы (имя, системное имя, роль):")
    if others:
        lines.extend(f"- {a.short()}" for a in others)
    else:
        lines.append("- (пока нет)")
    return "\n".join(lines)
