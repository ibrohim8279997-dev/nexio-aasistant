"""Запуск одного агента: python run_agent.py <имя_агента>.

Коды выхода (их понимает run.sh): 0 — штатная остановка, 1 — падение (перезапуск),
2 — ошибка конфигурации (без перезапуска), 3 — агент выключен или не найден (без перезапуска).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.agents.bus import Bus
from bot.agents.context import AgentContext
from bot.agents.health import AgentState, Heartbeat
from bot.agents.registry import NAME_RE, AgentConfig, Registry
from bot.ai.llm import make_llm
from bot.config import BASE_DIR, ConfigError, Settings, load_settings
from bot.crypto import Crypto, DecryptError, SecretStore
from bot.db import Database
from bot.logging_setup import setup_logging

log = logging.getLogger("bot")

EXIT_OK, EXIT_CRASH, EXIT_CONFIG, EXIT_DISABLED = 0, 1, 2, 3
TOKEN_SECRET = "bot_token:{name}"


class AgentDisabled(Exception):
    pass


async def resolve_token(agent: AgentConfig, settings: Settings, secrets: SecretStore) -> str | None:
    """Токен бота: переменная из .env (KIMO_TOKEN и т.п.) -> зашифрованный secrets -> BOT_TOKEN (только kimo)."""
    token = os.getenv(agent.token_env, "").strip()
    if token:
        return token
    try:
        token = await secrets.get(TOKEN_SECRET.format(name=agent.name))
    except DecryptError:
        raise ConfigError(f"Токен {agent.name} в secrets не расшифровывается (сменился ENCRYPTION_KEY?)") from None
    if token:
        return token
    if agent.kind == "kimo" and settings.bot_token:
        return settings.bot_token
    return None


async def run(name: str, settings: Settings, crypto: Crypto) -> None:
    db = await Database.connect(settings.db_path)
    try:
        await db.migrate()
        registry = Registry(db)
        await registry.seed_defaults()
        agent = await registry.get(name)
        if agent is None:
            raise AgentDisabled(f"Агент {name} не найден в таблице agents")
        if not agent.enabled:
            raise AgentDisabled(f"Агент {name} выключен (enabled=0)")
        if agent.config.get("draft") or agent.config.get("awaiting_name") or agent.config.get("awaiting_token"):
            raise AgentDisabled(f"Агент {name} не готов: нужны подтверждённое имя и токен бота")

        token = await resolve_token(agent, settings, SecretStore(db, crypto))
        if not token:
            raise ConfigError(
                f"Нет токена для {name}: задайте {agent.token_env} в .env или "
                f"python manage.py agents set-token {name}"
            )
        # токен агента тоже вырезаем из логов
        setup_logging(settings.log_level, [*settings.secret_values, token], BASE_DIR / "logs", name)

        llm = make_llm(settings, agent.provider, agent.text_model, agent.stt_model)
        bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        bus = Bus(db, agent.name, owner_id=settings.admin_id, display=agent.display_name)
        state = AgentState(started_at=time.time())
        ctx = AgentContext(agent=agent, registry=registry, system_db=db, bus=bus, state=state)
        interrupted = await db.queue_fail_interrupted(agent.name, _now())
        if interrupted:
            log.warning("Помечено прерванных заданий: %s", interrupted)
        llm.on_rate_limit = rate_notifier(bot, settings.admin_id, agent.display_name)  # type: ignore[attr-defined]
        heartbeat = Heartbeat(db, agent.name, state, llm)
        await heartbeat.beat()  # сразу online: Кимо видит живой процесс, не дожидаясь первого интервала
        heartbeat_task = asyncio.create_task(heartbeat.run())
        try:
            if agent.kind == "kimo":
                from bot.main import run_kimo

                await run_kimo(settings, ctx, llm, bot, crypto)
            else:
                from bot.agents.generic import run_generic

                await run_generic(settings, ctx, llm, bot)
        finally:
            heartbeat.stop()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await bot.session.close()
            await llm.close()
    finally:
        await db.close()
        log.info("Агент %s остановлен", name)


def rate_notifier(bot: Bot, owner_id: int, display: str):
    """Сообщение владельцу о лимите Groq (429): кто, сколько ждать, удалось ли выполнить запрос."""

    async def notify(wait: float, exhausted: bool) -> None:
        text = f"⏳ {display}: лимит Groq (429). Повтор запроса примерно через {int(wait)} с."
        if exhausted:
            text += " Этот запрос выполнить не удалось, попробуйте позже."
        await bot.send_message(owner_id, text, parse_mode=None)

    return notify


def _now() -> str:
    from bot.timeutil import now_utc, to_db

    return to_db(now_utc())


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not NAME_RE.match(argv[0]):
        print("Использование: python run_agent.py <имя_агента>  (латиница, цифры, _)", file=sys.stderr)
        return EXIT_CONFIG
    name = argv[0]
    try:
        settings = load_settings()
        crypto = Crypto(settings.encryption_key)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(settings.log_level, settings.secret_values, BASE_DIR / "logs", name)
    try:
        asyncio.run(run(name, settings, crypto))
    except (KeyboardInterrupt, SystemExit):
        return EXIT_OK
    except AgentDisabled as exc:
        log.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return EXIT_DISABLED
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 — логируем через фильтр секретов, run.sh перезапустит
        log.critical("Агент %s упал: %s", name, type(exc).__name__, exc_info=exc)
        return EXIT_CRASH
    return EXIT_OK
