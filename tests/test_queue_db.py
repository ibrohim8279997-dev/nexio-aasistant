import asyncio

from bot.db import MIGRATIONS, Database

NOW = "2026-11-08 10:00:00"


async def test_migration_from_v1_backs_up_and_keeps_data(tmp_path):
    db = await Database.connect(tmp_path / "bot.db")
    # имитируем старую БД версии 1 с данными
    await db.conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    user = await db.get_or_create_user(1, "a", "A", NOW)
    await db.add_expense(user["id"], 5000, "UZS", "Еда", None, 12000, NOW)
    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS)
    assert len(list((tmp_path / "backups").glob("*before-v2.db"))) == 1
    assert len(await db.expenses_between(user["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00")) == 1

    await db.downgrade(1)
    assert await db.schema_version() == 1
    assert await db._one("SELECT name FROM sqlite_master WHERE name = 'tasks_queue'") is None
    assert len(await db.expenses_between(user["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00")) == 1
    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS)
    await db.close()


async def test_parallel_migrate_with_lock(tmp_path):
    dbs = [await Database.connect(tmp_path / "bot.db") for _ in range(3)]
    await asyncio.gather(*(d.migrate() for d in dbs))
    assert all([await d.schema_version() == len(MIGRATIONS) for d in dbs])
    for d in dbs:
        await d.close()


async def test_queue_claim_is_atomic_across_connections(tmp_path):
    a = await Database.connect(tmp_path / "bot.db")
    await a.migrate()
    b = await Database.connect(tmp_path / "bot.db")
    for i in range(20):
        await a.queue_insert("kimo", "assistant", {"n": i}, NOW)

    async def worker(db):
        got = []
        while (task := await db.queue_claim("assistant", NOW)) is not None:
            got.append(task["id"])
        return got

    r1, r2 = await asyncio.gather(worker(a), worker(b))
    assert len(r1) + len(r2) == 20 and not set(r1) & set(r2)
    assert await a.queue_claim("assistant", NOW) is None
    await a.close()
    await b.close()


async def test_agents_crud(db):
    await db.insert_agent({"name": "x", "role": "тест", "models": {"text": "m"}, "allowed_tables": ["tasks"]}, NOW)
    assert not await db.insert_agent({"name": "x", "role": "другое"}, NOW)  # seed не перезаписывает
    agent = await db.get_agent("x")
    assert agent["role"] == "тест" and agent["models"] == {"text": "m"} and agent["enabled"] is False
    await db.update_agent("x", enabled=True, allowed_tables=[])
    assert (await db.list_agents(enabled_only=True))[0]["allowed_tables"] == []
