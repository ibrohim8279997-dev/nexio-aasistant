from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import Database  # noqa: E402


@pytest.fixture
async def db(tmp_path: Path):
    database = await Database.connect(tmp_path / "test.db")
    await database.migrate()
    yield database
    await database.close()


from bot.config import Settings  # noqa: E402
from bot.timeutil import now_utc, to_db  # noqa: E402
from bot.crypto import Crypto, SecretStore, generate_key  # noqa: E402

ADMIN_ID = 1000


class FakeGroq:
    """Подменяет GroqClient: возвращает заранее заданные ответы."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.transcript = ""
        self.prompts: list[list[dict]] = []
        self.calls: list[dict] = []

    text_model = "openai/gpt-oss-120b"
    stt_model = "whisper-large-v3-turbo"

    async def chat_json(self, messages, max_tokens=2048):
        self.prompts.append(messages)
        return self.replies.pop(0)

    async def chat(self, messages, *, json_mode=False, search=False, max_tokens=2048, agent=""):
        from bot.ai.groq_client import ChatResult

        self.prompts.append(messages)
        self.calls.append({"json_mode": json_mode, "search": search, "agent": agent})
        return ChatResult(self.replies.pop(0))

    async def close(self):
        return None

    async def transcribe(self, audio, filename="voice.ogg"):
        return self.transcript


def attach_admin(ctx, bot, settings, log_dir=None):
    """Слой admin/подтверждений для агента (как в run_kimo)."""
    from bot.agents.admin_actions import AdminActions
    from bot.agents.admin_ops import AdminOps
    from bot.agents.confirm import Confirmations

    ops = AdminOps(ctx.system_db, ctx.agent.name, log_dir)
    confirm = Confirmations(ctx.system_db, ctx.agent.name, ops, settings.admin_id, online_timeout=3.0)
    ctx.admin = AdminActions(ctx, ops, confirm, bot)
    return ctx.admin


class FakeRates:
    """Курс без сети: фиксированный 12 000 сум за $."""

    def __init__(self, db, rate: float | None = 12000.0) -> None:
        from bot.services.rates import RateInfo

        self.info = RateInfo(rate=rate, date="2026-11-07") if rate else None

    async def get_usd_rate(self):
        from bot.services.rates import RateUnavailableError

        if self.info is None:
            raise RateUnavailableError("нет")
        return self.info

    async def try_get(self):
        return self.info


def make_settings(tmp_path, **over) -> Settings:
    values = dict(
        bot_token="123456:" + "A" * 35,
        groq_api_key="gsk_test",
        admin_id=ADMIN_ID,
        encryption_key=generate_key(),
        text_model="openai/gpt-oss-120b",
        stt_model="whisper-large-v3-turbo",
        db_path=tmp_path / "bot.db",
        click_enabled=False,
        subscription_enabled=True,
        search_enabled=True,
        click_provider_token="",
        rate_limit_per_min=100,
        sub_price_xtr=250,
        sub_days=30,
        click_price_uzs=49000,
        log_level="INFO",
    )
    values.update(over)
    return Settings(**values)


@pytest.fixture
def groq():
    return FakeGroq()


@pytest.fixture
async def app(tmp_path, db, groq):
    """Диспетчер с фейковыми зависимостями."""
    from bot.handlers import get_routers
    from bot.main import build_dispatcher
    from tests.fakebot import FakeBot

    for router in get_routers():  # роутеры модульные — отцепляем от прошлого теста
        router._parent_router = None

    from bot.agents.access import ScopedDB
    from bot.agents.context import AgentContext
    from bot.agents.registry import Registry

    settings = make_settings(tmp_path)
    registry = Registry(db)
    await registry.seed_defaults()
    kimo = await registry.get("kimo")
    for a in await registry.all():  # все агенты «живы»: свежий heartbeat
        await db.agent_heartbeat(a.name, "online", to_db(now_utc()))
    kimo = await registry.get("kimo")
    scoped = ScopedDB(db, kimo.name, kimo.allowed_tables, live=True)
    secrets = SecretStore(db, Crypto(settings.encryption_key))
    from bot.agents.bus import Bus

    ctx = AgentContext(agent=kimo, registry=registry, system_db=db, bus=Bus(db, "kimo", owner_id=ADMIN_ID))
    bot = FakeBot()
    attach_admin(ctx, bot, settings)
    deps = dict(db=scoped, secrets=secrets, groq=groq, rates=FakeRates(scoped), agent_ctx=ctx, confirm=ctx.admin.confirm)
    dp = build_dispatcher(settings, **deps)
    return dp, bot, settings
