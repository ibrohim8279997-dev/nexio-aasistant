"""Этап 1: роли и права, доходы, изоляция данных, очередь LLM и лимит на пользователя."""
from __future__ import annotations

import asyncio

import pytest

from bot.ai.groq_client import GroqRateLimitError
from bot.ai.queue import P_BACKGROUND, P_CLIENT, P_OWNER, LLMQueue, QueuedLLM, UserLimitError, UserLimiter
from bot.db import MIGRATIONS, Database
from bot.services import expenses as exp
from bot.services import perms
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID, FakeRates

NOW = lambda: to_db(now_utc())  # noqa: E731


async def make_user(db, tg_id):
    return await db.ensure_user(tg_id, NOW())


# ---------- миграция ----------

async def test_migration4_up_down_idempotent(tmp_path):
    db = await Database.connect(tmp_path / "m.db")
    await db.migrate()
    await db.migrate()  # повторный запуск ничего не ломает
    assert await db.schema_version() == len(MIGRATIONS)
    u = await make_user(db, 5)
    await db.add_expense(u["id"], 10, "UZS", "Еда", None, None, NOW())
    await db.downgrade(3)
    assert await db.schema_version() == 3
    cols = [c["name"] for c in await db.table_columns("expenses")]
    assert "kind" not in cols
    row = await db._one("SELECT COUNT(*) AS n FROM expenses")
    assert row["n"] == 1  # данные расходов сохранены
    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS)
    assert list((tmp_path / "backups").glob("*.db"))  # бэкапы перед миграцией
    await db.close()


# ---------- доходы и изоляция ----------

async def test_income_and_isolation(db):
    a, b = await make_user(db, 1), await make_user(db, 2)
    rates = FakeRates(db)
    await exp.create_expense(db, rates, a["id"], 5_000_000, "UZS", "Зарплата", None, "income")
    await exp.create_expense(db, rates, a["id"], 100, "USD", "Еда", None)
    await exp.create_expense(db, rates, b["id"], 999, "USD", "Секрет", None, "income")
    start, end = "2000-01-01 00:00:00", "2100-01-01 00:00:00"
    assert [r["category"] for r in await db.expenses_between(a["id"], start, end)] == ["Еда"]
    assert [r["category"] for r in await db.expenses_between(a["id"], start, end, "income")] == ["Зарплата"]
    assert len(await db.expenses_between(a["id"], start, end, None)) == 2
    assert await db.expenses_between(b["id"], start, end) == []  # чужое не видно
    # удалить чужую запись нельзя
    other = (await db.expenses_between(b["id"], start, end, "income"))[0]
    assert await db.delete_expense(a["id"], other["id"]) is False
    with pytest.raises(ValueError):
        await db.add_expense(a["id"], 1, "UZS", "x", None, None, NOW(), "gift")


async def test_summary_shows_income_and_balance(db):
    a = await make_user(db, 1)
    rates = FakeRates(db)
    await exp.create_expense(db, rates, a["id"], 1_000_000, "UZS", "Еда", None)
    await exp.create_expense(db, rates, a["id"], 3_000_000, "UZS", "Зарплата", None, "income")
    text = await exp.summary_text(db, rates, a["id"], "month")
    assert "Доходы" in text and "Баланс" in text and "2 000 000" in text


async def test_summary_income_only(db):
    a = await make_user(db, 1)
    await exp.create_expense(db, FakeRates(db), a["id"], 50, "USD", "Продажа", None, "income")
    assert "Расходов нет" in (text := await exp.summary_text(db, FakeRates(db), a["id"], "day"))
    assert "Доходы" in text


# ---------- права ----------

async def test_default_perms_and_admin_only(db):
    u = await make_user(db, 7)
    admin = await make_user(db, ADMIN_ID)
    for p in perms.USER_PERMS:
        assert await perms.can(db, u, p, ADMIN_ID)
    for p in perms.ADMIN_ONLY:
        assert not await perms.can(db, u, p, ADMIN_ID)
        assert await perms.can(db, admin, p, ADMIN_ID)
    assert not await perms.can(db, u, "что-то-левое", ADMIN_ID)


async def test_admin_perms_cannot_be_granted(db):
    u = await make_user(db, 7)
    with pytest.raises(perms.PermError):
        await perms.set_perm(db, u, "agents_manage", True)
    await db.set_user_perm(u["id"], "agents_manage", True, NOW())  # даже запись в БД не даёт права
    assert not await perms.can(db, u, "agents_manage", ADMIN_ID)


async def test_perm_toggle(db):
    u = await make_user(db, 7)
    await perms.set_perm(db, u, "finance", False)
    assert not await perms.can(db, u, "finance", ADMIN_ID)
    assert await perms.can(db, u, "tasks", ADMIN_ID)
    await perms.set_perm(db, u, "finance", True)
    assert await perms.can(db, u, "finance", ADMIN_ID)


async def test_assistant_blocks_actions_without_perm():
    from bot.ai.actions import AddExpense, CreateAgent, ManageAgent
    from bot.services.assistant import needed_perm

    assert needed_perm(AddExpense(type="add_expense", amount=1)) == "finance"
    assert needed_perm(CreateAgent(type="create_agent", description="агент для X")) == "agents_manage"
    assert needed_perm(ManageAgent(type="manage_agent", agent="a", op="stop")) == "agents_manage"


async def test_assistant_execute_denied(db):
    from bot.ai.actions import AddExpense
    from bot.services import assistant

    u = await make_user(db, 7)
    deps = assistant.AssistantDeps(db=db, user=u, rates=FakeRates(db), perms=frozenset({"tasks"}))
    [ans] = await assistant.execute([AddExpense(type="add_expense", amount=5)], deps)
    assert "недоступна" in ans.text
    assert await db.expenses_between(u["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00") == []


# ---------- очередь LLM ----------

class SlowLLM:
    text_model = "m"
    stt_model = "s"

    def __init__(self):
        self.order = []
        self.fail_times = 0
        self.calls = 0

    async def chat_json(self, messages, max_tokens=2048):
        self.calls += 1
        if self.fail_times:
            self.fail_times -= 1
            raise GroqRateLimitError(0.0)
        self.order.append(messages[0]["content"])
        await asyncio.sleep(0.01)
        return "{}"

    async def close(self):
        pass


async def test_queue_priority_order():
    inner = SlowLLM()
    q = LLMQueue(concurrency=1, retry_delays=(0.0,))
    root = QueuedLLM(inner, q)
    blocker = asyncio.create_task(root.chat_json([{"content": "first"}]))
    await asyncio.sleep(0)  # первый занял воркер
    tasks = [
        asyncio.create_task(root.bound(P_BACKGROUND).chat_json([{"content": "bg"}])),
        asyncio.create_task(root.bound(P_CLIENT).chat_json([{"content": "client"}])),
        asyncio.create_task(root.bound(P_OWNER).chat_json([{"content": "owner"}])),
    ]
    await asyncio.gather(blocker, *tasks)
    assert inner.order == ["first", "owner", "client", "bg"]
    await q.close()


async def test_queue_retries_429_for_background_not_owner():
    inner = SlowLLM()
    q = LLMQueue(concurrency=1, retry_delays=(0.0, 0.0))
    root = QueuedLLM(inner, q)
    inner.fail_times = 2
    assert await root.bound(P_BACKGROUND).chat_json([{"content": "x"}]) == "{}"
    assert inner.calls == 3
    inner.fail_times, inner.calls = 1, 0
    with pytest.raises(GroqRateLimitError):  # владельцу ошибка отдаётся сразу
        await root.bound(P_OWNER).chat_json([{"content": "y"}])
    assert inner.calls == 1
    inner.fail_times, inner.calls = 5, 0
    with pytest.raises(GroqRateLimitError):  # повторы кончились
        await root.bound(P_CLIENT).chat_json([{"content": "z"}])
    assert inner.calls == 3
    await q.close()


async def test_queue_daily_429_not_retried():
    class Daily(SlowLLM):
        async def chat_json(self, messages, max_tokens=2048):
            self.calls += 1
            raise GroqRateLimitError(7200, daily=True)

    inner = Daily()
    q = LLMQueue(retry_delays=(0.0,))
    with pytest.raises(GroqRateLimitError):
        await QueuedLLM(inner, q, P_CLIENT).chat_json([{"content": "x"}])
    assert inner.calls == 1
    await q.close()


async def test_user_limiter_per_user():
    t = [0.0]
    lim = UserLimiter(2, 3, clock=lambda: t[0])
    lim.check(1)
    lim.check(1)
    with pytest.raises(UserLimitError) as e:
        lim.check(1)
    assert not e.value.daily and "Подождите" in e.value.user_message
    lim.check(2)  # другой пользователь не затронут
    t[0] = 61
    lim.check(1)  # минута прошла, но это уже 3-й запрос за сутки
    t[0] = 122
    with pytest.raises(UserLimitError) as e:
        lim.check(1)
    assert e.value.daily
    t[0] = 90000
    lim.check(1)


async def test_user_limit_applies_through_queue():
    q = LLMQueue(limiter=UserLimiter(1, 10), retry_delays=())
    root = QueuedLLM(SlowLLM(), q)
    user = root.bound(P_CLIENT, owner=5)
    await user.chat_json([{"content": "a"}])
    with pytest.raises(UserLimitError):
        await user.chat_json([{"content": "b"}])
    await root.chat_json([{"content": "owner"}])  # у владельца лимита нет
    await q.close()
