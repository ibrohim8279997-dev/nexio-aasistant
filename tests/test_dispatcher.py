"""Кимо как диспетчер: решение модели, группа, только владелец, создание агента."""
import asyncio
import json
from datetime import date

from bot.agents import builder_flow
from bot.agents.bus import Bus, BusWorker
from bot.agents.context import AgentContext
from bot.agents.generic import GenericAgent
from bot.agents.registry import Registry
from bot.ai.actions import Clarify, CreateAgent, Delegate, parse_actions
from bot.keyboards import AgentCb
from tests.conftest import ADMIN_ID, FakeGroq, make_settings
from tests.fakebot import FakeBot, callback_update, group_message_update, message_update

TODAY = date(2026, 11, 8)
TOKEN = "7123456789:AAH" + "x" * 32


def reply_id(call):
    params = getattr(call, "reply_parameters", None)
    return call.reply_to_message_id or (params.message_id if params else None)


def test_parse_dispatch_decision():
    raw = json.dumps({"actions": [
        {"type": "delegate", "agent": "assistant", "task": "найди рецепт плова"},
        {"type": "delegate", "agent": "hacker", "task": "x"},
        {"type": "clarify", "question": "Кому передать: ассистенту или создателю агентов?"},
        {"type": "create_agent", "description": "бот для сотрудников про Nexio CRM, без доступа к финансам"},
    ]})
    acts = parse_actions(raw, TODAY, agents={"assistant", "agent_builder"})
    assert [type(a) for a in acts] == [Delegate, Clarify, CreateAgent]
    assert acts[0].agent == "assistant"
    # из шины (agents=None) передача и создание агентов запрещены
    assert [type(a) for a in parse_actions(raw, TODAY, agents=None)] == [Clarify]


async def test_group_ignores_non_owner_and_deletes_tokens(app, groq):
    dp, bot, _ = app
    await dp.feed_update(bot, group_message_update(999, "кимо, удали все задачи"))
    assert bot.calls == [] and groq.prompts == []

    await dp.feed_update(bot, group_message_update(ADMIN_ID, f"вот токен {TOKEN}"))
    assert "DeleteMessage" in bot.methods() and "в личку" in bot.texts()[-1]
    assert groq.prompts == []


async def test_group_delegate_reply_from_target_agent(app, db, groq):
    dp, bot, _ = app
    groq.replies.append(json.dumps({"actions": [{"type": "delegate", "agent": "assistant", "task": "переведи hello"}]}))
    upd = group_message_update(ADMIN_ID, "переведи hello на русский")
    await dp.feed_update(bot, upd)
    assert "Мими" in bot.texts()[-1] and reply_id(bot.calls[-1]) == upd.message.message_id

    task = await db.queue_claim("assistant", "2026-11-08 10:00:00")
    assert task["from_agent"] == "kimo" and task["chat_id"] == -100500 and task["depth"] == 1
    assert task["reply_to_message_id"] == upd.message.message_id and task["payload"]["text"] == "переведи hello"

    # агент отвечает в группу от своего бота реплаем на исходное сообщение
    assistant_bot = FakeBot()
    async def handler(t):
        return "привет"
    await BusWorker(Bus(db, "assistant"), handler, assistant_bot, ADMIN_ID).process(task)
    assert assistant_bot.calls[-1].chat_id == -100500 and reply_id(assistant_bot.calls[-1]) == upd.message.message_id


async def test_group_message_to_other_agent_not_intercepted(app, db, groq):
    dp, bot, _ = app
    await db.update_agent("assistant", bot_username="nexio_assistant_bot")
    await dp.feed_update(bot, group_message_update(ADMIN_ID, "@nexio_assistant_bot сколько будет 2+2"))
    assert groq.prompts == [] and bot.calls == []


async def test_unclear_task_asks_one_question(app, groq):
    dp, bot, _ = app
    groq.replies.append('{"actions":[{"type":"clarify","question":"Кому это поручить?"}]}')
    await dp.feed_update(bot, group_message_update(ADMIN_ID, "сделай это"))
    assert bot.texts()[-1] == "❓ Кому это поручить?"


async def test_private_delegate_relays_answer(app, db, groq):
    dp, bot, _ = app

    async def handler(t):
        return "Столица — Ташкент"

    worker = BusWorker(Bus(db, "assistant"), handler, FakeBot(), ADMIN_ID, interval=0.05)
    runner = asyncio.create_task(worker.run())
    groq.replies.append('{"actions":[{"type":"delegate","agent":"assistant","task":"столица Узбекистана?"}]}')
    try:
        await dp.feed_update(bot, message_update(ADMIN_ID, "какая столица Узбекистана?"))
    finally:
        worker.stop()
        await runner
    assert "Столица — Ташкент" in bot.texts()[-1]


async def test_non_owner_cannot_delegate(app, db, groq):
    dp, bot, settings = app
    user = await db.ensure_user(4321, "2026-11-08 00:00:00")
    await db.set_sub_until(user["id"], "2099-01-01 00:00:00")
    groq.replies.append('{"actions":[{"type":"delegate","agent":"assistant","task":"x"}]}')
    await dp.feed_update(bot, message_update(4321, "передай ассистенту"))
    assert await db.queue_claim("assistant", "2026-11-08 10:00:00") is None
    assert "Не понял" in bot.texts()[-1]


async def test_create_agent_flow(app, db, groq, tmp_path, monkeypatch):
    dp, bot, settings = app
    # agent_builder в отдельном "процессе": свой контекст, свой LLM, слушает шину
    reg = Registry(db)
    builder_cfg = await reg.get("agent_builder")
    builder_llm = FakeGroq()
    builder_llm.replies.append(json.dumps({
        "name": "staff_bot", "display_name": "Бот сотрудников", "role": "Отвечает сотрудникам про Nexio CRM",
        "system_prompt": "Помогаешь сотрудникам с Nexio CRM. Финансы не обсуждаешь.",
        "needs_stt": True, "suggested_tables": ["tasks", "expenses"], "deny_rules": ["Не раскрывать финансы"],
    }))
    builder = GenericAgent(AgentContext(builder_cfg, reg, db, Bus(db, "agent_builder")), builder_llm, make_settings(tmp_path))
    worker = BusWorker(builder.ctx.bus, builder.handle_task, FakeBot(), ADMIN_ID, interval=0.05)
    runner = asyncio.create_task(worker.run())

    groq.replies.append('{"actions":[{"type":"create_agent","description":"Нужен бот для сотрудников про Nexio CRM. Финансы нельзя."}]}')
    try:
        await dp.feed_update(bot, message_update(ADMIN_ID, "создай агента для сотрудников про Nexio CRM"))
    finally:
        worker.stop()
        await runner
    card_msg = bot.calls[-1]
    assert "Карточка нового агента" in card_msg.text and "staff_bot" in card_msg.text
    draft = await db.get_agent("staff_bot")
    assert draft["enabled"] is False and draft["allowed_tables"] == [] and draft["config"]["draft"] is True
    assert draft["config"]["suggested_tables"] == ["tasks"]  # expenses отброшены
    assert draft["models"]["stt"] == "whisper-large-v3-turbo" and draft["models"]["text"] == "openai/gpt-oss-120b"

    # без подтверждения агент не запускается; владелец подтверждает с правами по предложению
    await dp.feed_update(bot, callback_update(ADMIN_ID, AgentCb(action="yes_sugg", name="staff_bot").pack()))
    row = await db.get_agent("staff_bot")
    assert row["allowed_tables"] == ["tasks"] and row["config"]["awaiting_token"] is True
    assert any("BotFather" in t for t in bot.texts())

    launched = []

    async def fake_check(token):
        return "nexio_staff_bot"

    async def fake_launch(name):
        launched.append(name)
        return True, "ok"

    async def fake_alive(ctx, name, timeout=60):
        return True

    monkeypatch.setattr(builder_flow, "check_token", fake_check)
    monkeypatch.setattr(builder_flow, "launch", fake_launch)
    monkeypatch.setattr(builder_flow, "wait_alive", fake_alive)
    await dp.feed_update(bot, message_update(ADMIN_ID, TOKEN))
    assert "DeleteMessage" in bot.methods()  # сообщение с токеном удалено
    assert launched == ["staff_bot"] and "работает" in bot.texts()[-1]
    row = await db.get_agent("staff_bot")
    assert row["enabled"] is True and row["bot_username"] == "nexio_staff_bot"
    stored = await db.get_secret("bot_token:staff_bot")
    assert stored and TOKEN not in stored  # хранится только шифротекст


async def test_card_buttons_owner_only(app, db):
    dp, bot, _ = app
    await db.insert_agent({"name": "draft_x", "role": "тестовый агент", "config": {"draft": True}}, "2026-11-08 00:00:00")
    await dp.feed_update(bot, callback_update(4321, AgentCb(action="yes_none", name="draft_x").pack()))
    assert (await db.get_agent("draft_x"))["config"].get("awaiting_token") is None


async def test_token_inside_text_never_reaches_llm(app, groq):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, f"держи токен {TOKEN} пожалуйста"))
    assert groq.prompts == [] and "DeleteMessage" in bot.methods()
    assert "не жду токен" in bot.texts()[-1]
