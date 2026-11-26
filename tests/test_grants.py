"""Этап 3: /grant, /revoke, /grants, автоотзыв, уведомления, доступ без выдачи."""
from __future__ import annotations

from datetime import timedelta

import pytest

from bot.db import MIGRATIONS, Database
from bot.middlewares import AccessMiddleware
from bot.services import grants
from bot.services.scheduler import BotScheduler
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID, make_settings
from tests.fakebot import message_update

NOW = lambda: to_db(now_utc())  # noqa: E731
U = 4242


@pytest.fixture(autouse=True)
def _reset():
    AccessMiddleware._requested.clear()


async def test_grant_command_creates_record_and_notifies(app, db):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 10"))
    user = await db.get_user_by_tg(U)
    row = await db.grant_active(user["id"])
    assert row and row["granted_by"] == ADMIN_ID and row["active"] == 1
    assert any(c.chat_id == U and "Вам открыт доступ" in c.text for c in bot.calls if type(c).__name__ == "SendMessage")
    await dp.feed_update(bot, message_update(ADMIN_ID, "/grants"))
    assert str(U) in bot.texts()[-1]


async def test_grant_extends_from_current_end(app, db):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 10"))
    user = await db.get_user_by_tg(U)
    first = (await db.grant_active(user["id"]))["expires_at"]
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 5"))
    second = await db.grant_active(user["id"])
    assert second["expires_at"] > first
    n = await db._one("SELECT COUNT(*) AS n FROM access_grants WHERE user_id = ?", (user["id"],))
    assert n["n"] == 1  # одна активная запись


async def test_grant_validation_and_admin_only(app, db):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 0"))
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 99999"))
    await dp.feed_update(bot, message_update(ADMIN_ID, "/grant abc 5"))
    user = await db.get_user_by_tg(U)
    assert user is None or await db.grant_active(user["id"]) is None
    # обычный пользователь и пользователь с выданным доступом не могут выдавать доступ
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 30"))
    await dp.feed_update(bot, message_update(U, "/grant 9999 30"))
    assert await db.get_user_by_tg(9999) is None
    await dp.feed_update(bot, message_update(U, f"/revoke {U}"))
    assert await db.grant_active((await db.get_user_by_tg(U))["id"]) is not None
    await dp.feed_update(bot, message_update(U, "/grants"))
    assert not any("Активные доступы" in t for t in bot.texts())


async def test_revoke_command(app, db):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 10"))
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/revoke {U}"))
    user = await db.get_user_by_tg(U)
    assert await db.grant_active(user["id"]) is None
    assert any(c.chat_id == U and "отключён" in c.text for c in bot.calls if type(c).__name__ == "SendMessage")
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/revoke {U}"))
    assert "нет активного доступа" in bot.texts()[-1]
    await dp.feed_update(bot, message_update(ADMIN_ID, "/revoke 31337"))
    assert "не найден" in bot.texts()[-1]


async def test_no_access_message_explains_and_notifies_admin_once(app, db):
    dp, bot, _ = app
    dp.workflow_data["settings"] = make_settings(app[2].db_path.parent, subscription_enabled=False)
    await dp.feed_update(bot, message_update(555, "привет"))
    await dp.feed_update(bot, message_update(555, "ещё раз"))
    mine = [c for c in bot.calls if type(c).__name__ == "SendMessage" and c.chat_id == 555]
    assert len(mine) == 2 and "ID" in mine[0].text and "555" in mine[0].text
    requests = [c for c in bot.calls if type(c).__name__ == "SendMessage" and c.chat_id == ADMIN_ID and "Запрос доступа" in c.text]
    assert len(requests) == 1 and "/grant 555 30" in requests[0].text


async def test_user_with_grant_has_access_and_expired_does_not(app, db):
    dp, bot, settings = app
    user = await db.ensure_user(U, NOW())
    now = now_utc()
    await grants.grant(db, user, 5, ADMIN_ID, now)
    assert await grants.has_access(db, user, settings, now)
    assert await grants.has_access(db, user, settings, now + timedelta(days=4))
    assert not await grants.has_access(db, user, settings, now + timedelta(days=6))  # срок вышел, даже без задачи автоотзыва
    assert await grants.has_access(db, {"id": 0, "tg_id": ADMIN_ID}, settings)  # админ всегда


async def test_grant_expiry_job_closes_and_notifies(app, db):
    dp, bot, settings = app
    user = await db.ensure_user(U, NOW())
    now = now_utc()
    await grants.grant(db, user, 1, ADMIN_ID, now - timedelta(days=3))  # уже истёк
    sched = BotScheduler(bot, db, settings)
    await sched.access_grants_job()
    assert await db.grant_active(user["id"]) is None
    closed = await db._one("SELECT * FROM access_grants WHERE user_id = ?", (user["id"],))
    assert closed["active"] == 0 and closed["end_reason"] == "expired"
    msgs = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
    assert any(c.chat_id == U and "закончился" in c.text for c in msgs)
    assert any(c.chat_id == ADMIN_ID and str(U) in c.text for c in msgs)
    n = len(msgs)
    await sched.access_grants_job()  # повторно ничего не шлёт
    assert len([c for c in bot.calls if type(c).__name__ == "SendMessage"]) == n


async def test_reminder_one_day_before_sent_once(app, db):
    dp, bot, settings = app
    user = await db.ensure_user(U, NOW())
    await grants.grant(db, user, 1, ADMIN_ID, now_utc() - timedelta(hours=1))  # осталось ~23 часа
    sched = BotScheduler(bot, db, settings)
    await sched.access_grants_job()
    await sched.access_grants_job()
    mine = [c for c in bot.calls if type(c).__name__ == "SendMessage" and c.chat_id == U and "заканчивается" in c.text]
    assert len(mine) == 1
    assert await db.grant_active(user["id"]) is not None  # ещё действует
    # продление снова разрешает напоминание
    await grants.grant(db, user, 1, ADMIN_ID)
    assert (await db.grant_active(user["id"]))["notified_1d"] == 0


async def test_far_future_grant_not_reminded(app, db):
    dp, bot, settings = app
    user = await db.ensure_user(U, NOW())
    await grants.grant(db, user, 30, ADMIN_ID)
    await BotScheduler(bot, db, settings).access_grants_job()
    assert not [c for c in bot.calls if type(c).__name__ == "SendMessage" and c.chat_id == U]


async def test_revoked_user_loses_business_and_reminders(app, db, groq):
    from tests.test_business import CONN, biz_update, conn_update, reply_json, sent_to

    dp, bot, _ = app
    user = await db.ensure_user(555, NOW())
    await grants.grant(db, user, 5, ADMIN_ID)
    await dp.feed_update(bot, conn_update(owner_tg=555, conn_id="c-555"))
    groq.replies.append(reply_json("ок"))
    await dp.feed_update(bot, biz_update(777, "привет", conn_id="c-555"))
    assert len(sent_to(bot, 777)) == 1
    await dp.feed_update(bot, message_update(ADMIN_ID, "/revoke 555"))
    await dp.feed_update(bot, biz_update(777, "ещё", conn_id="c-555"))
    assert len(sent_to(bot, 777)) == 1  # после отзыва Кимо клиентам не отвечает


async def test_old_sub_until_grants_migrated(tmp_path):
    db = await Database.connect(tmp_path / "m.db")
    for n in range(1, 6):
        await db.conn.executescript(f"BEGIN;\n{MIGRATIONS[n - 1]}\nPRAGMA user_version = {n};\nCOMMIT;")
    future = to_db(now_utc() + timedelta(days=5))
    past = to_db(now_utc() - timedelta(days=5))
    for tg, until in ((1, future), (2, past), (3, None)):
        await db._write("INSERT INTO users (tg_id, created_at, sub_until) VALUES (?, ?, ?)", (tg, NOW(), until))
    await db.migrate()
    rows = await db._all("SELECT user_id FROM access_grants")
    assert [r["user_id"] for r in rows] == [(await db.get_user_by_tg(1))["id"]]
    await db.downgrade(5)
    assert await db._one("SELECT name FROM sqlite_master WHERE name = 'access_grants'") is None
    await db.close()


async def test_subscription_status_shows_grant(app, db):
    dp, bot, _ = app
    dp.workflow_data["settings"] = make_settings(app[2].db_path.parent, subscription_enabled=False)
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/grant {U} 10"))
    await dp.feed_update(bot, message_update(U, "/subscription"))
    assert "Доступ открыт" in bot.texts()[-1]
