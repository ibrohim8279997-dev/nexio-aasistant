"""Кимо: диспетчер aiogram и запуск процесса. Точка входа агентов — run_agent.py."""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, ErrorEvent

from bot.agents.access import ScopedDB
from bot.agents.bus import BusWorker
from bot.agents.context import AgentContext
from bot.agents.kimo import KimoBusHandler
from bot.ai.llm import LLMClient
from bot.config import Settings
from bot.crypto import Crypto, SecretStore
from bot.handlers import get_routers
from bot.middlewares import AccessMiddleware, ThrottlingMiddleware, UserMiddleware
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
    BotCommand(command="paysupport", description="Помощь с оплатой"),
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
    access = AccessMiddleware()  # inner: видит флаги хендлеров (public)
    dp.message.middleware(access)
    dp.callback_query.middleware(access)

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


async def run_kimo(settings: Settings, ctx: AgentContext, llm: LLMClient, bot: Bot, crypto: Crypto) -> None:
    """Процесс Кимо: прежний бот (задачи, расходы, статистика, оплата) + шина заданий + диспетчер."""
    agent = ctx.agent
    scoped = ScopedDB(ctx.system_db, agent.name, agent.allowed_tables)  # права Кимо на таблицы
    secrets = SecretStore(scoped, crypto)  # type: ignore[arg-type]
    rates = RatesService(scoped)  # type: ignore[arg-type]

    dp = build_dispatcher(settings, db=scoped, secrets=secrets, groq=llm, rates=rates, agent_ctx=ctx)
    scheduler = BotScheduler(bot, scoped, settings, system_db=ctx.system_db)  # type: ignore[arg-type]
    worker = BusWorker(ctx.bus, KimoBusHandler(ctx, scoped, llm, rates, settings), bot, settings.admin_id)
    worker_task: asyncio.Task[None] | None = None
    try:
        scheduler.start()
        try:
            await bot.set_my_commands(COMMANDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось установить команды: %s", type(exc).__name__)
        try:
            me = await bot.get_me()
            if me.username != agent.bot_username:
                await ctx.system_db.update_agent(agent.name, bot_username=me.username)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить данные бота: %s", type(exc).__name__)
        worker_task = asyncio.create_task(worker.run())
        log.info("Кимо запущен (long polling)")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.stop()
        if worker_task:
            await asyncio.gather(worker_task, return_exceptions=True)
        scheduler.shutdown()
        await rates.close()


def main() -> None:
    """Совместимость: python -m bot.main == python run_agent.py kimo."""
    from bot.runner import main as runner_main

    sys.exit(runner_main(["kimo"]))


if __name__ == "__main__":
    main()
