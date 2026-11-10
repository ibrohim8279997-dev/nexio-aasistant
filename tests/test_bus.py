import asyncio
import json

import pytest

from bot.agents.bus import MAX_DEPTH, Bus, BusError, BusWorker, DepthExceeded
from bot.agents.context import AgentContext
from bot.agents.generic import GenericAgent, PlanError, parse_plan
from bot.agents.registry import Registry
from tests.conftest import ADMIN_ID, FakeGroq, make_settings
from tests.fakebot import FakeBot


async def test_depth_limit(db):
    await db.migrate()
    bus = Bus(db, "kimo")
    task_id = await bus.send("assistant", {"type": "ask", "text": "x"}, depth=MAX_DEPTH)
    task = await db.queue_get(task_id)
    assert task["depth"] == 3 and task["status"] == "new"

    with pytest.raises(DepthExceeded) as info:
        await Bus(db, "assistant").forward(task, "agent_builder")  # 4-я передача
    stopped = await db.queue_get(info.value.task_id)
    assert stopped["status"] == "failed" and "больше 3" in stopped["result"]


async def test_send_validation(db):
    bus = Bus(db, "kimo")
    with pytest.raises(BusError):
        await bus.send("kimo", {})  # самому себе
    with pytest.raises(BusError):
        await bus.send("Bad Name", {})


async def test_worker_replies_in_chat_from_own_bot(db):
    sender, receiver = Bus(db, "kimo"), Bus(db, "assistant")
    task_id = await sender.send(
        "assistant", {"type": "ask", "text": "q", "deliver_by": "receiver"}, chat_id=-100, reply_to=42
    )

    async def handler(task):
        return "ответ ассистента"

    bot = FakeBot()
    worker = BusWorker(receiver, handler, bot, ADMIN_ID)
    await worker.process(await receiver.claim())
    task = await db.queue_get(task_id)
    assert task["status"] == "done" and task["result"] == "ответ ассистента"
    sent = bot.calls[-1]
    assert sent.chat_id == -100 and sent.reply_parameters.message_id == 42 and sent.text == "ответ ассистента"


async def test_worker_notifies_owner_on_depth_stop(db):
    bus = Bus(db, "assistant")
    await Bus(db, "kimo").send("assistant", {"type": "ask", "text": "q", "reply": False}, depth=3)

    async def handler(task):
        return await bus.forward(task, "agent_builder")

    bot = FakeBot()
    await BusWorker(bus, handler, bot, ADMIN_ID).process(await bus.claim())
    assert bot.calls[-1].chat_id == ADMIN_ID and "остановлена" in bot.calls[-1].text


async def test_ask_roundtrip(db):
    kimo, assistant = Bus(db, "kimo"), Bus(db, "assistant")

    async def kimo_handler(task):
        return f"эхо: {task['payload']['text']}"

    worker = BusWorker(kimo, kimo_handler, FakeBot(), ADMIN_ID, interval=0.05)
    runner = asyncio.create_task(worker.run())
    try:
        assert await assistant.ask("kimo", "напоминания?", timeout=5) == "эхо: напоминания?"
    finally:
        worker.stop()
        await runner


async def test_ask_timeout(db):
    with pytest.raises(BusError):
        await Bus(db, "assistant").ask("kimo", "x", timeout=0.2)


def test_parse_plan():
    agents = {"kimo"}
    assert parse_plan('{"action":"answer","text":"42"}', agents, False).text == "42"
    assert parse_plan('{"action":"ask_agent","agent":"kimo","question":"задачи?"}', agents, False).agent == "kimo"
    assert parse_plan('{"action":"search","query":"курс"}', agents, True).query == "курс"
    for raw in ('{"action":"search","query":"x"}', '{"action":"ask_agent","agent":"evil","question":"x"}',
                '{"action":"rm"}', "не json", '{"action":"answer"}'):
        with pytest.raises(PlanError):
            parse_plan(raw, agents, False)


async def _agent(db, tmp_path, name, **settings_over):
    reg = Registry(db)
    await reg.seed_defaults()
    cfg = await reg.get(name)
    ctx = AgentContext(agent=cfg, registry=reg, system_db=db, bus=Bus(db, name))
    llm = FakeGroq()
    return GenericAgent(ctx, llm, make_settings(tmp_path, **settings_over)), llm


async def test_assistant_search_flag(db, tmp_path):
    agent, llm = await _agent(db, tmp_path, "assistant")
    llm.replies += ['{"action":"search","query":"погода Ташкент"}', "Солнечно 【1†L2】"]
    assert await agent.answer("какая погода?") == "Солнечно"
    assert llm.calls[1]["search"] is True and "не больше 2 поисков" in llm.prompts[1][0]["content"]

    agent_off, llm_off = await _agent(db, tmp_path, "assistant", search_enabled=False)
    llm_off.replies += ['{"action":"search","query":"x"}', "без поиска"]
    assert await agent_off.answer("погода?") == "без поиска"  # план с поиском отклонён -> прямой ответ
    assert all(not c["search"] for c in llm_off.calls)


async def test_assistant_asks_kimo_for_reminders(app, db, tmp_path):
    """Ассистент спрашивает Кимо через шину и возвращает готовый ответ."""
    from bot.agents.kimo import KimoBusHandler
    from bot.agents.access import ScopedDB
    from tests.conftest import FakeRates

    dp, bot, settings = app
    reg = Registry(db)
    kimo_cfg = await reg.get("kimo")
    kimo_db = ScopedDB(db, "kimo", kimo_cfg.allowed_tables, live=True)
    kimo_llm = FakeGroq()
    kimo_llm.replies.append('{"actions":[{"type":"add_expense","amount":5,"currency":"USD"},{"type":"list_tasks","day":"today"}]}')
    owner = await kimo_db.ensure_user(ADMIN_ID, "2026-11-08 00:00:00")
    await db.add_task(owner["id"], "позвонить в банк", "2026-11-08 05:00:00", True, "2026-11-08 00:00:00")
    handler = KimoBusHandler(AgentContext(kimo_cfg, reg, db, Bus(db, "kimo")), kimo_db, kimo_llm, FakeRates(kimo_db), settings)
    worker = BusWorker(Bus(db, "kimo"), handler, FakeBot(), ADMIN_ID, interval=0.05)
    runner = asyncio.create_task(worker.run())

    agent, llm = await _agent(db, tmp_path, "assistant")
    llm.replies += ['{"action":"ask_agent","agent":"kimo","question":"какие у меня напоминания?"}', "У вас: позвонить в банк"]
    try:
        assert await agent.answer("какие у меня напоминания?") == "У вас: позвонить в банк"
    finally:
        worker.stop()
        await runner
    assert "позвонить в банк" in llm.prompts[-1][0]["content"]  # данные от Кимо попали в итоговый запрос
    assert await db.expenses_between(owner["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00") == []  # расход из шины не выполнен


async def test_builder_card(db, tmp_path):
    agent, llm = await _agent(db, tmp_path, "agent_builder")
    llm.replies.append(json.dumps({
        "name": "Staff Bot", "display_name": "Бот сотрудников", "role": "Отвечает сотрудникам про Nexio CRM",
        "system_prompt": "Ты помогаешь сотрудникам разобраться в Nexio CRM.", "needs_stt": False,
        "suggested_tables": ["expenses", "tasks", "secrets"], "deny_rules": ["Не раскрывать финансы"],
    }))
    card = json.loads(await agent.handle_task({"id": 1, "depth": 1, "payload": {"type": "build_card", "text": "бот для сотрудников"}}))
    assert card["name"] == "staff_bot" and card["suggested_tables"] == ["tasks"]

    other, _ = await _agent(db, tmp_path, "assistant")
    with pytest.raises(BusError):
        await other.handle_task({"id": 2, "depth": 1, "payload": {"type": "build_card", "text": "x"}})
