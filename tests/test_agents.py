import pytest

from bot.agents.access import AccessDenied, ScopedDB, can_access
from bot.agents.registry import AgentConfig, Registry, identity_block, load_default_configs
from bot.ai.actions import build_messages

NOW = "2026-11-08 10:00:00"


def test_default_cards_valid():
    names = {c["name"] for c in load_default_configs()}
    assert {"kimo", "agent_builder", "assistant"} <= names


def test_can_access_rules():
    assert can_access("kimo", ["expenses"], "expenses", "admin")
    assert not can_access("assistant", ["expenses"], "expenses")  # расходы только у Кимо, даже если в конфиге
    assert not can_access("staff_bot", [], "tasks")  # по умолчанию ничего
    assert can_access("staff_bot", [], "agent_staff_bot_kb")  # свои таблицы
    assert not can_access("staff_bot", [], "agent_other_kb")


async def test_scoped_db_enforces_tables(db):
    kimo = ScopedDB(db, "kimo", ["users", "tasks", "expenses"], role="admin")
    user = await kimo.get_or_create_user(1, None, "A", NOW)
    await kimo.add_expense(user["id"], 1000, "UZS", "Еда", None, None, NOW)

    assistant = ScopedDB(db, "assistant", ["users", "tasks", "expenses"])
    with pytest.raises(AccessDenied):
        await assistant.expenses_between(user["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00")
    with pytest.raises(AccessDenied):
        assistant.conn  # noqa: B018 — прямой доступ к соединению запрещён
    with pytest.raises(AccessDenied):
        assistant._write  # noqa: B018

    newbie = ScopedDB(db, "newbie", [])
    with pytest.raises(AccessDenied):
        await newbie.open_tasks(user["id"])


async def test_registry_identity(db):
    reg = Registry(db)
    assert set(await reg.seed_defaults()) >= {"kimo", "assistant", "agent_builder"}
    assert await reg.seed_defaults() == []  # повторно не добавляет
    kimo = await reg.get("kimo")
    prompt = await reg.identity_prompt(kimo)
    assert "Тебя зовут Кимо" in prompt and "диспетчер" in prompt
    assert "assistant" in prompt and "agent_builder" in prompt and "(kimo)" not in prompt.split("Другие агенты")[1]

    await db.update_agent("assistant", enabled=False)
    assert "assistant" not in (await reg.identity_prompt(kimo)).split("Другие агенты")[1]


def test_identity_in_messages():
    agent = AgentConfig(name="kimo", role="диспетчер", config={"display_name": "Кимо"})
    msgs = build_messages("как тебя зовут?", "2026-11-08 10:00", [], identity_block(agent, []))
    assert msgs[0]["content"].startswith("Тебя зовут Кимо")


async def test_kimo_knows_name_end_to_end(app, groq):
    from tests.conftest import ADMIN_ID
    from tests.fakebot import message_update

    dp, bot, _ = app
    groq.replies.append('{"actions":[{"type":"reply","text":"Кимо"}]}')
    await dp.feed_update(bot, message_update(ADMIN_ID, "как тебя зовут?"))
    assert "Тебя зовут Кимо" in groq.prompts[0][0]["content"]
    assert bot.texts()[-1] == "Кимо"
