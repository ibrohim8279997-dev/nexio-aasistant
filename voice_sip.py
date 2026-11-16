"""Голосовой SIP-сервис: принимает звонки владельца из Linphone по Tailscale и звонит ему по заданиям Кимо.

    python voice_sip.py             — запуск службы (из окружения с pjsua2, обычно ~/sipenv)
    python voice_sip.py --selftest  — проверка на самом сервере: пароль, 403, звук, кодеки (без Linphone)

Только звонок и звук: решения принимает Кимо через очередь заданий. Коды выхода как у агентов:
0 — штатно, 1 — падение, 2 — ошибка конфигурации, 3 — служба выключена."""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys

from bot.agents.health import AgentState, Heartbeat
from bot.agents.registry import Registry
from bot.config import BASE_DIR, ConfigError, load_settings
from bot.db import Database
from bot.logging_setup import setup_logging
from bot.voice.config import VoiceConfig
from bot.voice.digest import AttemptGuard, DigestAuthenticator, IncomingGate
from bot.agents.bus import Bus
from bot.ai.llm import make_llm
from bot.voice.asr import Recognizer
from bot.voice.kimo_link import BusKimoLink, BusRelay
from bot.voice.outbound import NoBot, OutboundCaller, ServiceWorker
from bot.voice.service import VoiceService
from bot.voice.session import CallSession, VoiceDeps
from bot.voice.tts import Synthesizer

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

        if shutil.which("ffmpeg") is None:
            log.error("ffmpeg не найден в PATH: озвучка работать не будет. В Debian (proot): apt install -y ffmpeg")
        loop = asyncio.get_running_loop()
        state = AgentState()
        heartbeat = Heartbeat(db, NAME, state)
        await heartbeat.beat()
        hb_task = asyncio.create_task(heartbeat.run())
        llm = make_llm(settings, "groq", None, agent.stt_model or settings.stt_model)  # один ключ Groq на все агенты
        bus = Bus(db, NAME, owner_id=settings.admin_id, display=agent.display_name)
        relay = BusRelay(bus, settings.admin_id)
        synth = Synthesizer(cfg.voice_ru, cfg.voice_uz)
        deps = VoiceDeps(Recognizer(llm), synth, None, relay, cfg)  # type: ignore[arg-type]
        service = VoiceService(cfg, db, state, lambda call, svc: CallSession(call, svc, deps))
        deps.kimo = BusKimoLink(bus, db, relay, service, cfg.answer_wait_seconds, settings.admin_id)
        warm_task = asyncio.create_task(synth.warm_up())  # заготовка «сейчас не могу ответить»
        gate = IncomingGate(DigestAuthenticator(cfg.sip_user, cfg.sip_password, cfg.realm), AttemptGuard(), service.busy)
        stack = Pjsua2Stack(cfg, gate, service.on_incoming, loop)
        caller = OutboundCaller(stack, service, deps)
        worker = ServiceWorker(bus, caller.place, NoBot(), settings.admin_id, state=state)  # type: ignore[arg-type]
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        worker_task: asyncio.Task[None] | None = None
        try:
            stack.start()
            worker_task = asyncio.create_task(worker.run())
            log.info("voice_sip запущен")
            await stop.wait()
        finally:
            worker.stop()
            if worker_task is not None:
                await asyncio.gather(worker_task, return_exceptions=True)
            stack.stop()
            heartbeat.stop()
            warm_task.cancel()
            await asyncio.gather(hb_task, warm_task, return_exceptions=True)
            await llm.close()
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
