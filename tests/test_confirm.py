"""Опасные действия только после «да» владельца в личке; права по ролям; аудит; без повторов."""
import json

import pytest

from bot.agents import procs
from bot.agents.admin_ops import AdminOps
from bot.agents.confirm import ConfirmError, Confirmations
from bot.agents.registry import Registry
from bot.keyboards import ConfirmCb
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID
from tests.fakebot import callback_update, group_message_update, message_update

NOW = "2026-11-10 10:00:00"


class FakeControl:
    """Подмена tmux: старт «поднимает» агента (heartbeat), стоп гасит."""

    def __init__(self, db):
        self.db, self.calls = db, []

    async def start(self, name):
        self.calls.append(("start", name))
        await self.db.agent_heartbeat(name, "online", to_db(now_utc()))
        return True, "ok"

    async def stop(self, name):
        self.calls.append(("stop", name))
        await self.db.agent_heartbeat(name, "offline", to_db(now_utc()))


@pytest.fixture
async def world(db):
    reg = Registry(db)
    await reg.seed_defaults()
    await db.insert_agent({"name": "midi", "display_name": "Миди", "role": "агент встреч", "enabled": True,
                           "created_by": "agent_builder", "allowed_tables": ["tasks"]}, NOW)
    control = FakeControl(db)
    procs.set_control(control)
    yield control
    procs.set_control(procs.TmuxControl())


def make(db, actor):
    ops = AdminOps(db, actor)
    return Confirmations(db, actor, ops, ADMIN_ID, online_timeout=2.0)


def confirm_dm(bot):
    """Последнее сообщение с кнопками «Да/Нет» (подтверждение в личку владельцу)."""
    return next(c for c in reversed(bot.calls) if getattr(c, "reply_markup", None) and "Подтвердите действие" in (c.text or ""))


def last_confirm_id(bot):
    return ConfirmCb.unpack(confirm_dm(bot).reply_markup.inline_keyboard[0][0].callback_data).id


async def test_nothing_happens_without_yes(app, db, groq, world):
    dp, bot, _ = app
    groq.replies.append(json.dumps({"actions": [{"type": "manage_agent", "agent": "миди", "op": "restart"}]}))
    await dp.feed_update(bot, group_message_update(ADMIN_ID, "перезапусти Миди"))
    assert world.calls == []  # без «да» процесс не тронут
    dm = confirm_dm(bot)
    assert dm.chat_id == ADMIN_ID and "Перезапустить агента Миди" in dm.text  # подтверждение — в личку
    assert "в личку" in bot.texts()[-1] and bot.calls[-1].chat_id != ADMIN_ID  # в группе — только короткое сообщение


async def test_owner_yes_button_executes_once_with_audit(app, db, groq, world):
    dp, bot, _ = app
    groq.replies.append(json.dumps({"actions": [{"type": "manage_agent", "agent": "midi", "op": "restart"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "перезапусти midi"))
    pid = last_confirm_id(bot)
    await dp.feed_update(bot, callback_update(ADMIN_ID, ConfirmCb(id=pid, yes=1).pack()))
    assert world.calls == [("stop", "midi"), ("start", "midi")]
    assert "перезапущен и онлайн" in bot.texts()[-1]
    entry = next(r for r in await db.audit_list(target="midi") if r["action"] == "restart")
    assert entry["actor"] == "kimo" and entry["confirmed_by"] == ADMIN_ID and entry["report_id"] == pid and entry["result"] == "ok"
    # повторное нажатие ничего не делает
    await dp.feed_update(bot, callback_update(ADMIN_ID, ConfirmCb(id=pid, yes=1).pack()))
    assert world.calls == [("stop", "midi"), ("start", "midi")]


async def test_only_owner_in_private_can_confirm(db, world):
    kimo = make(db, "kimo")
    prop = await kimo.propose("stop", "midi")
    with pytest.raises(ConfirmError):
        await kimo.decide(prop.id, True, user_id=999, private=True)  # не владелец
    with pytest.raises(ConfirmError):
        await kimo.decide(prop.id, True, user_id=ADMIN_ID, private=False)  # не личка
    other = make(db, "agent_builder")
    with pytest.raises(ConfirmError):
        await other.decide(prop.id, True, ADMIN_ID, True)  # подтверждение чужого агента
    assert world.calls == []
    assert "Отменено" in await kimo.decide(prop.id, False, ADMIN_ID, True)
    with pytest.raises(ConfirmError):
        await kimo.decide(prop.id, True, ADMIN_ID, True)  # после отмены выполнить нельзя


async def test_expired_confirmation_rejected(db, world):
    kimo = make(db, "kimo")
    prop = await kimo.propose("stop", "midi")
    await db._write("UPDATE pending_actions SET expires_at = '2020-01-01 00:00:00' WHERE id = ?", (prop.id,))
    with pytest.raises(ConfirmError):
        await kimo.decide(prop.id, True, ADMIN_ID, True)
    assert world.calls == []


async def test_text_yes_confirms_single_pending(app, db, groq, world):
    dp, bot, _ = app
    groq.replies.append(json.dumps({"actions": [{"type": "manage_agent", "agent": "Миди", "op": "stop"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "останови Миди"))
    await dp.feed_update(bot, message_update(ADMIN_ID, "да"))
    assert world.calls == [("stop", "midi")] and (await db.get_agent("midi"))["enabled"] is False
    assert groq.replies == []  # «да» не ушло в LLM


async def test_permission_expansion_is_separate_and_limited(db, world):
    kimo = make(db, "kimo")
    prop = await kimo.propose("grant_tables", "midi", {"value": ["rates_cache"]})
    assert prop.expands is True and "РАСШИРЕНИЕ ПРАВ" in prop.summary
    for bad in (["expenses"], ["secrets"], ["payments"], ["users"]):
        with pytest.raises(ConfirmError):
            await kimo.propose("grant_tables", "midi", {"value": bad})
    await kimo.decide(prop.id, True, ADMIN_ID, True)
    assert (await db.get_agent("midi"))["allowed_tables"] == ["rates_cache", "tasks"]
    revoke = await kimo.propose("revoke_tables", "midi", {"value": ["tasks"]})
    assert revoke.expands is False
    await kimo.decide(revoke.id, True, ADMIN_ID, True)
    assert (await db.get_agent("midi"))["allowed_tables"] == ["rates_cache"]


async def test_creator_renames_own_agent_denied_for_foreign(db, world):
    """7.7: Мимо меняет имя созданного им агента после подтверждения и получает отказ на чужого."""
    mimo = make(db, "agent_builder")
    prop = await mimo.propose("set_display_name", "midi", {"value": "Миди Плюс"})
    assert (await db.get_agent("midi"))["display_name"] == "Миди"  # до «да» не изменилось
    assert "Имя изменено" in await mimo.decide(prop.id, True, ADMIN_ID, True)
    assert (await db.get_agent("midi"))["display_name"] == "Миди Плюс"
    from bot.agents.admin_ops import AdminError
    for foreign in ("assistant", "kimo"):
        with pytest.raises(AdminError):
            await mimo.propose("set_display_name", "assistant" if foreign == "assistant" else "kimo", {"value": "Хак"})
    kimo = make(db, "kimo")  # Кимо может менять имя любого
    p2 = await kimo.propose("set_display_name", "assistant", {"value": "Мими"})
    await kimo.decide(p2.id, True, ADMIN_ID, True)
    renames = [r for r in await db.audit_list(limit=50) if r["action"] == "set_display_name"]
    assert {r["actor"] for r in renames} == {"agent_builder", "kimo"} and all(r["confirmed_by"] == ADMIN_ID for r in renames)
    assert any(r["result"] == "denied" for r in await db.audit_list(limit=50))


async def test_display_name_validation(db, world):
    kimo = make(db, "kimo")
    for bad in ("бот, который назначает встречи", "x", "Мими", "Кимо", "a" * 40):
        with pytest.raises(ConfirmError):
            await kimo.propose("set_display_name", "midi", {"value": bad})  # описание, слишком коротко, занято, длинно


async def test_creator_cannot_touch_core_tables_or_write(db, world):
    from bot.agents.admin_ops import AdminError

    mimo = make(db, "agent_builder")
    with pytest.raises(AdminError):
        await mimo.propose("write_row", "midi", {"table": "agent_midi_x", "row_id": 1, "column": "a", "value": "1"})
    kimo = make(db, "kimo")
    with pytest.raises(ConfirmError):
        await kimo.propose("write_row", "midi", {"table": "expenses", "row_id": 1, "column": "amount", "value": 1})
    with pytest.raises(ConfirmError):
        await kimo.propose("restart", "kimo")  # себя Кимо не перезапускает


async def test_admin_write_row_only_after_yes(db, world):
    await db._write("CREATE TABLE agent_midi_meetings (id INTEGER PRIMARY KEY, title TEXT)")
    await db._write("INSERT INTO agent_midi_meetings (title) VALUES ('старое')")
    kimo = make(db, "kimo")
    prop = await kimo.propose("write_row", "midi", {"table": "agent_midi_meetings", "row_id": 1, "column": "title", "value": "новое"})
    assert (await db._one("SELECT title FROM agent_midi_meetings"))["title"] == "старое"
    await kimo.decide(prop.id, True, ADMIN_ID, True)
    assert (await db._one("SELECT title FROM agent_midi_meetings"))["title"] == "новое"


async def test_offline_agent_offers_restart_with_confirmation(app, db, groq, world):
    dp, bot, _ = app
    await db._write("UPDATE agents SET status = 'offline' WHERE name = 'midi'")
    groq.replies.append(json.dumps({"actions": [{"type": "delegate", "agent": "Миди", "task": "назначь встречу"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "передай Миди назначить встречу"))
    assert "Миди offline" in bot.texts()[-1] and "не передано" in bot.texts()[-1]
    assert confirm_dm(bot).chat_id == ADMIN_ID and "Перезапустить агента Миди" in confirm_dm(bot).text
    assert world.calls == []  # перезапуск только после «да»
    assert await db.queue_claim("midi", NOW) is None
