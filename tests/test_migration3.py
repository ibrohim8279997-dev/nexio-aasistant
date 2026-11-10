"""Миграция 3 на «боевой» БД версии 2: ничего не теряется, имена и роли проставляются, старые результаты не доставляются."""
import json

from bot.db import MIGRATIONS, Database

NOW = "2026-11-10 10:00:00"


async def old_db(tmp_path):
    db = await Database.connect(tmp_path / "bot.db")
    for number in (1, 2):
        await db.conn.executescript(f"BEGIN;\n{MIGRATIONS[number - 1]}\nPRAGMA user_version = {number};\nCOMMIT;")
    return db


async def test_migration3_preserves_and_backfills(tmp_path):
    db = await old_db(tmp_path)
    user = await db.get_or_create_user(1, "a", "A", NOW)
    await db.add_expense(user["id"], 5000, "UZS", "Еда", None, 12000, NOW)
    for name, role, cfg in [
        ("kimo", "Главный агент", {"display_name": "Кимо"}),
        ("agent_builder", "Создатель", {"display_name": "Создатель агентов"}),
        ("assistant", "бот-ассистент", {"display_name": "Ассистент"}),
        ("midi", "бот, который назначает встречи", {"display_name": "бот, который назначает встречи"}),
        ("shorty", "агент заметок", {"display_name": "Заметки"}),
    ]:
        await db._write(
            "INSERT INTO agents (name, role, allowed_tables, config, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, role, json.dumps(["users", "secrets", "expenses"] if name == "kimo" else []), json.dumps(cfg), NOW),
        )
    cur = await db._write(  # схема v2: без delivered и reply
        "INSERT INTO tasks_queue (from_agent, to_agent, payload, status, result, depth, created_at, updated_at) "
        "VALUES ('assistant', 'kimo', '{}', 'done', 'старый ответ', 1, ?, ?)", (NOW, NOW),
    )
    old_task = cur.lastrowid

    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS)
    assert list((tmp_path / "backups").glob("*before-v3.db"))  # бэкап перед миграцией

    agents = {a["name"]: a for a in await db.list_agents()}
    assert agents["kimo"]["display_name"] == "Кимо" and agents["kimo"]["access_role"] == "admin"
    assert agents["agent_builder"]["display_name"] == "Мимо" and agents["agent_builder"]["access_role"] == "creator"
    assert agents["assistant"]["display_name"] == "Мими" and agents["assistant"]["access_role"] == "agent"
    assert "agent_assistant_notes" in agents["assistant"]["allowed_tables"]
    assert agents["kimo"]["allowed_tables"] == ["users", "expenses"]  # secrets убраны
    # описание-вместо-имени не становится именем; прежнее описание сохранено
    assert agents["midi"]["display_name"] is None
    assert agents["midi"]["role_description"] == "бот, который назначает встречи" and agents["midi"]["role"] == "бот, который назначает встречи"
    assert agents["midi"]["created_by"] == "agent_builder" and agents["kimo"]["created_by"] is None
    assert agents["shorty"]["display_name"] == "Заметки"

    # данные и очередь не тронуты, старый результат не будет доставлен повторно
    assert len(await db.expenses_between(user["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00")) == 1
    old = await db.queue_get(old_task)
    assert old["result"] == "старый ответ" and old["delivered"] == 1 and old["reply"] == 0
    assert await db.queue_undelivered("assistant") == []
    await db.close()


async def test_migration3_downgrade_roundtrip(tmp_path):
    db = await old_db(tmp_path)
    await db._write("INSERT INTO agents (name, role, created_at) VALUES ('kimo', 'r', ?)", (NOW,))
    await db.migrate()
    await db.downgrade(2)
    assert await db.schema_version() == 2
    cols = {c["name"] for c in await db._all("PRAGMA table_info(agents)")}
    assert "display_name" not in cols and "name" in cols
    assert (await db._one("SELECT role FROM agents WHERE name = 'kimo'"))["role"] == "r"  # данные агента на месте
    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS)
    await db.close()


async def test_audit_and_pending_tables(db):
    audit_id = await db.audit(NOW, "kimo", "agent_restart", "midi", {"why": "test"}, confirmed_by=1)
    assert (await db.audit_list(target="midi"))[0]["id"] == audit_id
    pid = await db.pending_create("kimo", "restart", "midi", {}, "Перезапустить Миди", NOW, "2026-11-10 10:15:00")
    assert await db.pending_take(pid, "confirmed", NOW) is True
    assert await db.pending_take(pid, "confirmed", NOW) is False  # повторное «да» не выполняется
    expired = await db.pending_create("kimo", "stop", "midi", {}, "s", NOW, "2026-11-10 09:00:00")
    assert await db.pending_take(expired, "confirmed", NOW) is False
