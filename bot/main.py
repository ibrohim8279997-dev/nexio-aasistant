"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent

from bot.ai.groq_client import GroqClient
from bot.config import BASE_DIR, ConfigError, Settings, load_settings
from bot.crypto import Crypto, SecretStore
from bot.db import Database
from bot.handlers import get_routers
from bot.logging_setup import setup_logging
from bot.middlewares import ThrottlingMiddleware, UserMiddleware
from bot.services.rates import RatesService
from bot.services.scheduler import BotScheduler

log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="today", description="Задачи на сегодня"),
    BotCommand(command="tomorrow", description="Задачи на завтра"),
    BotCommand(command="add", description="Добавить задачу"),
    BotCommand(command="spend", description="Добавить расход"),
    BotCommand(command="subscription", description="Подписка"),
    BotCommand(command="help", description="Помощь"),
]


def build_dispatcher(settings: Settings, **deps: Any) -> Dispatcher:
    """deps попадают в хендлеры по имени аргумента: db, secrets, groq, rates, ..."""
    dp = Dispatcher()
    dp["settings"] = settings
    dp.workflow_data.update(deps)

    throttling = ThrottlingMiddleware(settings.rate_limit_per_min, settings.admin_id)
    dp.message.outer_middleware(throttling)
    dp.callback_query.outer_middleware(throttling)
    user_mw = UserMiddleware()
    dp.message.outer_middleware(user_mw)
    dp.callback_query.outer_middleware(user_mw)
    dp.pre_checkout_query.outer_middleware(user_mw)

    dp.include_routers(*get_routers())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Ошибка при обработке апдейта: %s", type(event.exception).__name__, exc_info=event.exception)
        update = event.update
        try:
            if update.message:
                await update.message.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз.")
            elif update.callback_query:
                await update.callback_query.answer("⚠️ Ошибка. Попробуйте ещё раз.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
        return True

    return dp


async def run_bot(settings: Settings, crypto: Crypto) -> None:
    db = await Database.connect(settings.db_path)
    await db.migrate()
    secrets = SecretStore(db, crypto)
    groq = GroqClient(settings.groq_api_key, settings.text_model, settings.stt_model)
    rates = RatesService(db)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(settings, db=db, secrets=secrets, groq=groq, rates=rates)
    scheduler = BotScheduler(bot, db, settings)
    try:
        scheduler.start()
        try:
            await bot.set_my_commands(COMMANDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось установить команды: %s", type(exc).__name__)
        log.info("Бот запущен (long polling)")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()
        await groq.close()
        await rates.close()
        await db.close()
        log.info("Бот остановлен")


def main() -> None:
    try:
        settings = load_settings()
        crypto = Crypto(settings.encryption_key)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level, settings.secret_values, BASE_DIR / "logs")
    try:
        asyncio.run(run_bot(settings, crypto))
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:  # noqa: BLE001 — логируем через фильтр секретов, run.sh перезапустит
        log.critical("Бот упал: %s", type(exc).__name__, exc_info=exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
