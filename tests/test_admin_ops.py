"""Админ-слой: Кимо (admin) читает всё кроме значений секретов, Мимо (creator) — только созданных им, agent — ничего."""
import pytest

from bot.agents.admin_ops import AdminError, AdminOps
from bot.agents.access import AccessDenied, ScopedDB
from bot.agents.registry import Registry

NOW = "2026-11-10 10:00:00"


@pytest.fixture
async def world(db, tmp_path):
    reg = Registry(db)
    await reg.seed_defaults()
    await db.insert_agent({"name": "midi", "display_name": "Миди", "role": "агент встреч", "enabled": True,
                           "created_by": "agent_builder", "allowed_tables": ["tasks", "expenses"]}, NOW)
    await db.insert_agent({"name": "zeta", "display_name": "Зета", "role": "чужой агент", "enabled": True,
                           "created_by": "kimo"}, NOW)
    await db._write("CREATE TABLE agent_midi_meetings (id INTEGER PRIMARY KEY, title TEXT, api_token TEXT)")
    await db._write("INSERT INTO agent_midi_meetings (title, api_token) VALUES ('Встреча с клиентом', 'SECRET-123')")
    await db._write("CREATE TABLE agent_zeta_notes (id INTEGER PRIMARY KEY, text TEXT)")
    await db._write("INSERT INTO agent_zeta_notes (text) VALUES ('заметка Зеты')")
    await db.set_secret("bot_token:midi", "gAAAAA-ciphertext")
    user = await db.get_or_create_user(1, "a", "A", NOW)
    await db.add_expense(user["id"], 5000, "UZS", "Еда", None, None, NOW)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "midi.log").write_text(
        "2026-11-10 09:00:00,001 INFO bot: старт\n"
        "2026-11-10 09:00:05,001 ERROR bot: ошибка с токеном 7123456789:AAH" + "x" * 32 + " и ключом gsk_" + "b" * 20 + "\n",
        encoding="utf-8",
    )
    return {k: AdminOps(db, k, logs) for k in ("kimo", "agent_builder", "assistant", "midi")}


async def test_admin_reads_structure_of_other_agent(world, db):
    kimo = world["kimo"]
    tables = await kimo.list_tables("midi")
    meetings = next(t for t in tables if t.name == "agent_midi_meetings")
    assert meetings.columns == ["id", "title", "api_token"] and meetings.owner == "midi"
    everything = {t.name for t in await kimo.list_tables()}
    assert {"expenses", "agent_midi_meetings", "agent_zeta_notes", "secrets"} <= everything
    # данные читаются, но колонка с токеном скрыта
    rows = await kimo.read_rows("midi", "agent_midi_meetings")
    assert rows[0]["title"] == "Встреча с клиентом" and rows[0]["api_token"] == "***"
    assert (await kimo.read_rows("midi", "expenses"))[0]["amount"] == 5000  # admin видит расходы
    # аудит записан
    assert {r["action"] for r in await db.audit_list(target="midi")} >= {"list_tables", "read_rows"}


async def test_secret_values_never_visible_even_to_admin(world):
    kimo = world["kimo"]
    assert await kimo.secret_names() == ["bot_token:midi"]
    with pytest.raises(AdminError):
        await kimo.read_rows("midi", "secrets")
    secrets_table = next(t for t in await kimo.list_tables() if t.name == "secrets")
    assert secrets_table.rows_hidden is True
    with pytest.raises(AdminError):
        await world["agent_builder"].secret_names()


async def test_creator_reads_own_agent_denied_for_others(world, db):
    mimo = world["agent_builder"]
    rows = await mimo.read_rows("midi", "agent_midi_meetings")
    assert rows[0]["title"] == "Встреча с клиентом"
    assert [a["name"] for a in await mimo.list_agents()] == ["midi"]  # только созданные им
    for target in ("zeta", "assistant", "kimo"):  # чужие агенты
        with pytest.raises(AdminError):
            await mimo.read_log(target)
    with pytest.raises(AdminError):
        await mimo.read_rows("zeta", "agent_zeta_notes")
    with pytest.raises(AdminError):
        await mimo.list_tables()  # общая структура БД — только admin
    with pytest.raises(AdminError):
        await mimo.read_rows("midi", "expenses")  # расходы недоступны, даже если выданы агенту в конфиге
    assert "expenses" not in {t.name for t in await mimo.list_tables("midi")}
    denied = [r for r in await db.audit_list(limit=50) if r["result"] == "denied" and r["actor"] == "agent_builder"]
    assert len(denied) >= 5  # отказы тоже пишутся в аудит


async def test_regular_agent_has_no_admin_rights(world):
    for actor in ("assistant", "midi"):
        ops = world[actor]
        for call in (ops.list_agents(), ops.list_tables("midi"), ops.read_log("midi"), ops.queue_overview()):
            with pytest.raises(AdminError):
                await call


async def test_roles_come_from_db_not_code(world, db):
    """Роль меняется в таблице agents — права меняются сразу, без правки кода."""
    with pytest.raises(AdminError):
        await world["assistant"].list_agents()
    await db.update_agent("assistant", access_role="admin")
    assert len(await world["assistant"].list_agents()) >= 4
    await db.update_agent("assistant", access_role="agent")
    with pytest.raises(AdminError):
        await world["assistant"].list_agents()


async def test_logs_are_redacted_and_filtered(world):
    lines = await world["kimo"].read_log("midi")
    text = "\n".join(lines)
    assert "AAH" not in text and "gsk_" not in text and "***" in text
    err = await world["kimo"].last_error_lines("midi")
    assert len(err) == 1 and "ошибка" in err[0]
    assert await world["kimo"].read_log("midi", minutes=1) == []  # за последнюю минуту записей нет (лог от 09:00)
    assert (await world["agent_builder"].read_log("midi"))  # Мимо видит лог созданного им агента


async def test_queue_overview_scoped(world, db):
    from bot.agents.bus import Bus

    bus = Bus(db, "kimo")
    t1 = await bus.send("midi", {"type": "ask", "text": "a"})
    t2 = await bus.send("zeta", {"type": "ask", "text": "b"})
    await db.queue_force_fail(t1, "⚠️ Миди offline", NOW)
    await db.queue_force_fail(t2, "⚠️ Зета offline", NOW)
    kimo_view = await world["kimo"].queue_overview()
    assert kimo_view["counts"]["midi"]["failed"] == 1 and kimo_view["counts"]["zeta"]["failed"] == 1
    mimo_view = await world["agent_builder"].queue_overview()
    assert set(mimo_view["counts"]) == {"midi"} and [f["to"] for f in mimo_view["failed"]] == ["midi"]


async def test_scoped_db_blocks_secrets_and_expenses_for_agents(db):
    kimo = ScopedDB(db, "kimo", ["expenses", "secrets"], live=False, role="admin")
    with pytest.raises(AccessDenied):
        await kimo.get_secret("x")  # даже у Кимо нет метода чтения значений секретов
    mimo = ScopedDB(db, "agent_builder", ["expenses"], role="creator")
    with pytest.raises(AccessDenied):
        await mimo.last_expense(1)
