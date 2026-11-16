"""Голосовой SIP-сервис: принимает звонки владельца из Linphone по Tailscale и звонит ему по заданиям Кимо.

    python voice_sip.py             — запуск службы (из окружения с pjsua2, обычно ~/sipenv)
    python voice_sip.py --selftest  — проверка на самом сервере: пароль, 403, звук, кодеки (без Linphone)

Только звонок и звук: решения принимает Кимо через очередь заданий. Коды выхода как у агентов:
0 — штатно, 1 — падение, 2 — ошибка конфигурации, 3 — служба выключена."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from bot.agents.health import AgentState, Heartbeat
from bot.agents.registry import Registry
from bot.config import BASE_DIR, ConfigError, load_settings
from bot.db import Database
from bot.logging_setup import setup_logging
from bot.voice.config import VoiceConfig
from bot.voice.digest import AttemptGuard, DigestAuthenticator, IncomingGate
from bot.voice.service import LoggingSession, VoiceService

log = logging.getLogger("voice")
NAME = "voice_sip"
EXIT_OK, EXIT_CRASH, EXIT_CONFIG, EXIT_DISABLED = 0, 1, 2, 3


async def amain(selftest: bool) -> int:
    settings = load_settings()
    cfg = VoiceConfig.from_env()
    setup_logging(settings.log_level, [*settings.secret_values, *cfg.secret_values], BASE_DIR / "logs", NAME)
    if selftest:
        from bot.voice.selftest import run_selftest

        return await run_selftest(cfg)

    db = await Database.connect(settings.db_path)
    try:
        await db.migrate()
        registry = Registry(db)
        await registry.seed_defaults()
        agent = await registry.get(NAME)
        if agent is None or not agent.enabled:
            print("Служба voice_sip выключена: python manage.py agents enable voice_sip", file=sys.stderr)
            return EXIT_DISABLED
        try:
            from bot.voice.pjsua_stack import Pjsua2Stack
        except ImportError as exc:  # pragma: no cover
            raise ConfigError(f"Не импортируется pjsua2: {exc}") from exc

        loop = asyncio.get_running_loop()
        state = AgentState()
        heartbeat = Heartbeat(db, NAME, state)
        await heartbeat.beat()
        hb_task = asyncio.create_task(heartbeat.run())
        service = VoiceService(cfg, db, state, LoggingSession)
        gate = IncomingGate(DigestAuthenticator(cfg.sip_user, cfg.sip_password, cfg.realm), AttemptGuard(), service.busy)
        stack = Pjsua2Stack(cfg, gate, service.on_incoming, loop)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            stack.start()
            log.info("voice_sip запущен")
            await stop.wait()
        finally:
            stack.stop()
            heartbeat.stop()
            await asyncio.gather(hb_task, return_exceptions=True)
        return EXIT_OK
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Голосовой SIP-сервис Кимо")
    parser.add_argument("--selftest", action="store_true", help="самопроверка на сервере")
    args = parser.parse_args()
    try:
        return asyncio.run(amain(args.selftest))
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 — run.sh перезапустит; секреты вырезает фильтр логов
        log.critical("voice_sip упал: %s", type(exc).__name__, exc_info=exc)
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
