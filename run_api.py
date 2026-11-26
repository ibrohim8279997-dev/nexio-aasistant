"""API для Mini App: python run_api.py. Слушает только локальный адрес; наружу его публикует Tailscale Funnel (см. README)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiohttp import web
from aiogram import Bot

from bot.agents.bus import Bus
from bot.agents.registry import Registry
from bot.api.app import ApiContext, create_app
from bot.config import BASE_DIR, ConfigError, load_settings
from bot.crypto import Crypto, SecretStore
from bot.db import Database
from bot.logging_setup import setup_logging
from bot.runner import resolve_token
from bot.services.rates import RatesService

log = logging.getLogger("api")


async def serve() -> None:
    settings = load_settings()
    crypto = Crypto(settings.encryption_key)
    db = await Database.connect(settings.db_path)
    try:
        await db.migrate()
        registry = Registry(db)
        await registry.seed_defaults()
        kimo = await registry.get("kimo")
        if kimo is None:
            raise ConfigError("В таблице agents нет Кимо")
        token = await resolve_token(kimo, settings, SecretStore(db, crypto))
        if not token:
            raise ConfigError("Нет токена Кимо: нужен для проверки initData (KIMO_TOKEN или manage.py agents set-token kimo)")
        setup_logging(settings.log_level, [*settings.secret_values, token], BASE_DIR / "logs", "api")
        bot = Bot(token)

        async def notify(tg_id: int, text: str) -> None:
            try:
                await bot.send_message(tg_id, text, parse_mode=None)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось отправить уведомление: %s", type(exc).__name__)

        ctx = ApiContext(
            settings=settings, db=db, bot_token=token, bus=Bus(db, "miniapp", owner_id=settings.admin_id),
            rates=RatesService(db), notify=notify, webapp_dir=BASE_DIR / "webapp", bot_username=kimo.bot_username or "",
        )
        host = os.getenv("API_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = int(os.getenv("API_PORT", "8080") or 8080)
        runner = web.AppRunner(create_app(ctx), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("API Mini App слушает %s:%s", host, port)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await ctx.rates.close()
            await bot.session.close()
    finally:
        await db.close()


def main() -> int:
    try:
        asyncio.run(serve())
    except (KeyboardInterrupt, SystemExit):
        return 0
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
