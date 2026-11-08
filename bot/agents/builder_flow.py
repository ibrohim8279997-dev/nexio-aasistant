"""Создание агента через Кимо: карточка от agent_builder -> подтверждение владельца -> токен в личке -> запуск.

Права выбирает владелец (кнопками или /agentperm). Модель только предлагает.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.agents.bus import BusError
from bot.agents.cards import GRANTABLE_TABLES, AgentCard, CardError, card_to_agent
from bot.agents.context import AgentContext
from bot.agents.registry import TOKEN_RE  # noqa: F401 — реэкспорт для хендлеров
from bot.config import BASE_DIR
from bot.crypto import SecretStore
from bot.runner import TOKEN_SECRET
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

BUILDER = "agent_builder"
CARD_TIMEOUT = 150.0
PING_TIMEOUT = 60.0


class FlowError(Exception):
    """Понятная владельцу ошибка в процессе создания агента."""


async def request_card(ctx: AgentContext, description: str) -> AgentCard:
    if await ctx.registry.get(BUILDER) is None:
        raise FlowError("Нет агента agent_builder в реестре.")
    try:
        raw = await ctx.bus.request(BUILDER, {"type": "build_card", "text": description}, timeout=CARD_TIMEOUT)
        return AgentCard.model_validate_json(raw)
    except BusError as exc:
        raise FlowError(f"Создатель агентов не ответил: {exc}") from exc
    except (ValueError, CardError) as exc:
        raise FlowError("Создатель агентов вернул некорректную карточку. Опишите агента подробнее.") from exc


async def save_draft(ctx: AgentContext, card: AgentCard) -> AgentCard:
    """Черновик в таблице agents (выключен, без прав). Имя делаем уникальным."""
    base, n = card.name, 1
    while await ctx.registry.get(card.name) is not None:
        n += 1
        card.name = f"{base[:28]}_{n}"
    row = card_to_agent(card, ctx.agent.models, tables=[], commands=[])
    row["config"].update(
        draft=True,
        suggested_tables=card.suggested_tables,
        suggested_commands=card.suggested_commands,
    )
    await ctx.system_db.insert_agent(row, to_db(now_utc()))
    return card


async def approve(ctx: AgentContext, name: str, with_suggested: bool) -> dict[str, Any]:
    row = await ctx.system_db.get_agent(name)
    if row is None or not row["config"].get("draft"):
        raise FlowError("Черновик агента не найден (возможно, уже подтверждён или отменён).")
    config = dict(row["config"])
    tables = [t for t in config.get("suggested_tables", []) if t in GRANTABLE_TABLES] if with_suggested else []
    commands = sorted({"start", "help", *(config.get("suggested_commands", []) if with_suggested else [])})
    config.update(draft=False, awaiting_token=True)
    await ctx.system_db.update_agent(name, allowed_tables=tables, allowed_commands=commands, config=config)
    row.update(allowed_tables=tables, allowed_commands=commands, config=config)
    return row


async def cancel(ctx: AgentContext, name: str) -> bool:
    row = await ctx.system_db.get_agent(name)
    if row is None or row["enabled"] or not (row["config"].get("draft") or row["config"].get("awaiting_token")):
        return False
    return await ctx.system_db.delete_agent(name)


async def set_tables(ctx: AgentContext, name: str, tables: list[str]) -> list[str]:
    row = await ctx.system_db.get_agent(name)
    if row is None or name == ctx.agent.name:
        raise FlowError("Нет такого агента (права Кимо меняются только в конфиге).")
    bad = [t for t in tables if t not in GRANTABLE_TABLES]
    if bad:
        raise FlowError(f"Нельзя выдать: {', '.join(bad)}. Доступно: {', '.join(sorted(GRANTABLE_TABLES))}")
    await ctx.system_db.update_agent(name, allowed_tables=tables)
    return tables


async def awaiting_token(ctx: AgentContext) -> list[str]:
    return [a.name for a in await ctx.registry.all() if a.config.get("awaiting_token")]


async def check_token(token: str) -> str:
    """Проверить токен через getMe, вернуть username бота."""
    probe = Bot(token)
    try:
        me = await probe.get_me()
    except TelegramAPIError as exc:
        raise FlowError(f"Telegram не принял токен ({type(exc).__name__}). Проверьте и пришлите снова.") from exc
    finally:
        await probe.session.close()
    if not me.username:
        raise FlowError("У бота нет username.")
    return me.username


async def accept_token(ctx: AgentContext, secrets: SecretStore, name: str, token: str) -> str:
    row = await ctx.system_db.get_agent(name)
    if row is None or not row["config"].get("awaiting_token"):
        raise FlowError("Этот агент не ждёт токен.")
    username = await check_token(token)
    for other in await ctx.registry.all():
        if other.name != name and other.bot_username and other.bot_username.lower() == username.lower():
            raise FlowError(f"Бот @{username} уже используется агентом {other.name}. Нужен новый бот.")
    await secrets.set(TOKEN_SECRET.format(name=name), token)
    config = {**row["config"], "awaiting_token": False}
    await ctx.system_db.update_agent(name, bot_username=username, enabled=True, config=config)
    return username


async def launch(name: str) -> tuple[bool, str]:
    """Запустить агента в новом окне tmux (./run.sh start <имя>)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(BASE_DIR / "run.sh"), "start", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(BASE_DIR),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (OSError, asyncio.TimeoutError) as exc:
        return False, type(exc).__name__
    return proc.returncode == 0, out.decode(errors="replace").strip()[-300:]


async def stop(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            str(BASE_DIR / "run.sh"), "stop", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, cwd=str(BASE_DIR),
        )
        await asyncio.wait_for(proc.wait(), timeout=15)
    except (OSError, asyncio.TimeoutError):
        log.warning("Не удалось остановить агента %s", name)


async def wait_alive(ctx: AgentContext, name: str, timeout: float = PING_TIMEOUT) -> bool:
    try:
        return await ctx.bus.request(name, {"type": "ping"}, timeout=timeout) == "pong"
    except BusError:
        return False
