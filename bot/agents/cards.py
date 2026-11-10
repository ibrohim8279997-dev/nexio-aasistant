"""Карточка нового агента: что предлагает agent_builder и что подтверждает владелец."""
from __future__ import annotations

import json
import re
from html import escape
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from bot.agents.access import RESTRICTED_TABLES
from bot.agents.registry import NAME_RE

# Таблицы, которые вообще можно предложить новому агенту (расходы, платежи, секреты — никогда)
GRANTABLE_TABLES = {"tasks", "rates_cache"}
MAX_LIST = 15


class CardError(ValueError):
    """Карточка от модели некорректна."""


class AgentCard(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    name: str
    display_name: str = Field(min_length=1, max_length=40)
    role: str = Field(min_length=5, max_length=500)
    system_prompt: str = Field(min_length=5, max_length=3000)
    needs_stt: bool = False
    needs_search: bool = False
    suggested_tables: list[str] = Field(default_factory=list)
    suggested_commands: list[str] = Field(default_factory=list)
    deny_rules: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        name = re.sub(r"[^a-z0-9_]", "_", str(v or "").strip().lower()).strip("_")
        if name and not name[0].isalpha():
            name = f"a_{name}"
        name = name[:32]
        if not NAME_RE.match(name):
            raise ValueError("некорректное имя агента")
        return name

    @field_validator("suggested_tables", "suggested_commands", "deny_rules", "questions", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        if not v:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip()[:300] for x in v if str(x).strip()][:MAX_LIST]

    @field_validator("suggested_tables")
    @classmethod
    def _tables(cls, v: list[str]) -> list[str]:
        # Модель только предлагает; запретные и неизвестные таблицы отбрасываем сразу
        return [t for t in v if t in GRANTABLE_TABLES and t not in RESTRICTED_TABLES]

    @field_validator("suggested_commands")
    @classmethod
    def _commands(cls, v: list[str]) -> list[str]:
        cleaned = [c.lstrip("/").lower() for c in v]
        return [c for c in cleaned if re.fullmatch(r"[a-z0-9_]{1,32}", c)]


def parse_card(raw: str) -> AgentCard:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise CardError("в ответе нет JSON")
    try:
        data = json.loads(text[start : end + 1])
        return AgentCard.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise CardError(f"карточка не прошла проверку: {type(exc).__name__}") from exc


def card_to_agent(card: AgentCard, models: dict[str, Any], tables: list[str], commands: list[str],
                  created_by: str = "agent_builder") -> dict[str, Any]:
    """Карточка + права, которые выбрал владелец -> запись для таблицы agents (выключена до запуска).

    Имя (display_name) модель только предлагает (config.suggested_name): записывается оно после вопроса владельцу."""
    return {
        "name": card.name,
        "display_name": None,
        "created_by": created_by,
        "access_role": "agent",
        "role": card.role,
        "role_description": card.role,
        "system_prompt": card.system_prompt,
        "models": {
            "provider": models.get("provider", "groq"),
            "text": models.get("text"),
            "stt": models.get("stt") if card.needs_stt else None,
            "search": bool(card.needs_search),
        },
        "allowed_tables": tables,
        "allowed_commands": sorted({"start", "help", *commands}),
        "deny_rules": card.deny_rules,
        "enabled": False,
        "config": {
            "suggested_name": card.display_name,
            "kind": "generic",
            "token_env": f"{card.name.upper()}_TOKEN",
            "audience": "owner",
        },
    }


def card_text(card: AgentCard) -> str:
    def items(values: list[str]) -> str:
        return ", ".join(escape(v) for v in values) if values else "—"

    lines = [
        "🧩 <b>Карточка нового агента</b>",
        f"Системное имя: <code>{card.name}</code>. Имя для общения вы зададите следующим шагом "
        f"(предложение создателя: «{escape(card.display_name)}»).",
        f"Роль: {escape(card.role)}",
        f"Голос (Whisper): {'да' if card.needs_stt else 'нет'} · Веб-поиск: {'да' if card.needs_search else 'нет'}",
        f"Запреты: {items(card.deny_rules)}",
        "",
        "<b>Права (решаете вы):</b>",
        f"Модель предлагает таблицы: {items(card.suggested_tables)}",
        f"Модель предлагает команды: {items(['/' + c for c in card.suggested_commands])}",
        "По умолчанию агент без доступа к таблицам. Расходы, платежи и секреты недоступны никогда.",
        "",
        f"<b>Системный промпт:</b>\n<i>{escape(card.system_prompt[:800])}</i>",
    ]
    if card.questions:
        lines += ["", "❓ Вопросы создателя агентов: " + items(card.questions)]
    return "\n".join(lines)


BUILDER_PROMPT = """{identity}

Задача: по описанию владельца составь карточку нового агента. Отвечай ТОЛЬКО JSON:
{{"name": "латиница_и_подчёркивания", "display_name": "Имя по-русски", "role": "кратко: что делает агент",
 "system_prompt": "промпт агента: стиль, задачи, ограничения", "needs_stt": true|false, "needs_search": true|false,
 "suggested_tables": [...], "suggested_commands": [...], "deny_rules": ["явные запреты"], "questions": ["что неясно"]}}

Правила:
- Только то, что есть в описании. Ничего не выдумывай. Неясно — пиши вопрос в questions.
- Таблицы можно предлагать только из списка: {tables}. Расходы, платежи, секреты — никогда.
- Конфиденциальность и запреты из описания перенеси в deny_rules и в system_prompt.
- Существующие агенты (имя не должно совпадать): {existing}"""


def builder_messages(identity: str, description: str, existing: list[str]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": BUILDER_PROMPT.format(
                identity=identity, tables=", ".join(sorted(GRANTABLE_TABLES)), existing=", ".join(existing) or "—"
            ),
        },
        {"role": "user", "content": description},
    ]
