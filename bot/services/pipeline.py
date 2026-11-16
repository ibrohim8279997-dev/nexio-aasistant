"""Общий путь обработки фразы владельца: текст -> Groq -> проверенные действия -> ответы.

Один и тот же путь используют Telegram (текст и голосовые сообщения) и голосовой звонок (через очередь заданий Кимо)."""
from __future__ import annotations

import logging

from bot.ai.actions import ActionParseError, build_messages, parse_actions
from bot.services import assistant
from bot.services.tasks import open_tasks_for_prompt
from bot.timeutil import context_now_text, now_local

log = logging.getLogger(__name__)


class NotUnderstood(Exception):
    """Модель не вернула ни одного допустимого действия."""


async def interpret(text: str, groq, deps: assistant.AssistantDeps,
                    history: list[tuple[str, str]] | None = None) -> list[assistant.Answer]:
    """Фраза -> ответы. Бросает GroqError (сервис недоступен) и NotUnderstood."""
    open_tasks = await open_tasks_for_prompt(deps.db, deps.user["id"])
    agents = None  # передавать задачи и управлять агентами может только владелец
    if deps.ctx is not None and deps.is_owner:
        agents = await deps.ctx.registry.aliases(exclude=deps.ctx.agent.name)
    raw = await groq.chat_json(build_messages(text, context_now_text(), open_tasks, deps.identity, history))
    try:
        actions = parse_actions(raw, now_local().date(), agents)
    except ActionParseError as exc:
        log.info("Не удалось разобрать ответ модели: %s", exc)
        raise NotUnderstood from exc
    if not actions:
        raise NotUnderstood
    return await assistant.execute(actions, deps)
