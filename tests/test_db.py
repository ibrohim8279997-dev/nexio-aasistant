from bot.db import MIGRATIONS

NOW = "2026-11-07 10:00:00"


async def test_migrations_idempotent(db):
    assert await db.schema_version() == len(MIGRATIONS)
    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS)


async def test_wal_enabled(db):
    row = await db._one("PRAGMA journal_mode")
    assert row["journal_mode"] == "wal"


async def test_user_data_isolated(db):
    a = await db.get_or_create_user(111, "a", "A", NOW)
    b = await db.get_or_create_user(222, "b", "B", NOW)
    task_id = await db.add_task(a["id"], "задача A", "2026-11-07 12:00:00", True, NOW)
    exp_id = await db.add_expense(a["id"], 1000, "UZS", "еда", None, 12000, NOW)

    # Пользователь B не видит и не может менять данные A
    assert await db.get_task(b["id"], task_id) is None
    assert not await db.complete_task(b["id"], task_id, NOW)
    assert not await db.delete_task(b["id"], task_id)
    assert not await db.delete_expense(b["id"], exp_id)
    assert await db.open_tasks(b["id"]) == []
    assert await db.expenses_between(b["id"], "2026-01-01 00:00:00", "2027-01-01 00:00:00") == []

    assert await db.complete_task(a["id"], task_id, NOW)
    assert len(await db.expenses_between(a["id"], "2026-01-01 00:00:00", "2027-01-01 00:00:00")) == 1


async def test_get_or_create_user_updates_names(db):
    u1 = await db.get_or_create_user(5, "old", "Old", NOW)
    u2 = await db.get_or_create_user(5, "new", "New", NOW)
    assert u1["id"] == u2["id"] and u2["username"] == "new"
    assert await db.count_users() == 1


async def test_duplicate_payment_ignored(db):
    u = await db.get_or_create_user(7, None, "X", NOW)
    assert await db.add_payment(u["id"], "charge-1", 250, NOW)
    assert not await db.add_payment(u["id"], "charge-1", 250, NOW)
    assert (await db.payments_summary())["n"] == 1
