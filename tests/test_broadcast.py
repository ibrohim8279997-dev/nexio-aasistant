"""Этап 4: рассылки по расписанию."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from bot.handlers import business as hb
from bot.services import broadcast as bc
from bot.services import grants
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID
from tests.fakebot import FakeBot, message_update
from tests.test_business import CONN, CLIENT, biz_update, conn_update, reply_json, sent_to

TASH = ZoneInfo("Asia/Tashkent")
NOW = lambda: to_db(now_utc())  # noqa: E731


@pytest.fixture(autouse=True)
def _reset():
    hb.reset_state()


def at(hour, minute=0, day=None):
    """Момент в Ташкенте (UTC+5) в понедельник 2026-11-16 по умолчанию."""
    return datetime(2026, 11, day or 16, hour, minute, tzinfo=TASH)


class Env:
    def __init__(self, app, db, groq):
        self.dp, self.bot, self.settings = app
        self.db, self.groq = db, groq
        self.now = at(10, 0)
        self.b = bc.Broadcaster(self.bot, db, self.settings, groq, clock=lambda: self.now, pause=self._nopause)

    async def _nopause(self, s):
        return None

    async def setup_owner(self):
        await self.dp.feed_update(self.bot, conn_update())
        self.owner = await self.db.get_user_by_tg(ADMIN_ID)

    async def client_wrote(self, ago=timedelta(hours=1), chat_id=CLIENT):
        await self.db.bchat_touch(self.owner["id"], chat_id, "Клиент", None, to_db(self.now - ago), True)

    async def schedule(self, mode="text", text="Как дела?", topic="", chat_id=CLIENT, t="10:00", days="1234567", channel=None):
        sid = await bc.create_schedule(self.db, self.owner, chat_id, t, days, mode, text if mode == "text" else "", topic, channel=channel)
        await self.db.sched_update(self.owner["id"], sid, **{})  if False else None
        # расписание создано «до» времени отправки
        await self.db._write("UPDATE scheduled_messages SET created_at = ? WHERE id = ?", (to_db(self.now - timedelta(days=1)), sid))
        return sid

    async def status(self, sid):
        return (await self.db.sched_get(self.owner["id"], sid))["last_status"]


@pytest.fixture
async def env(app, db, groq):
    e = Env(app, db, groq)
    await e.setup_owner()
    return e


# ---------- разбор ----------

def test_parse_days_and_time():
    assert bc.parse_days("daily") == "1234567" and bc.parse_days("weekdays") == "12345"
    assert bc.parse_days("пн,ср,пт") == "135" and bc.parse_days("1-3") == "123" and bc.parse_days("weekend") == "67"
    for bad in ("", "8", "xyz", "5-1"):
        with pytest.raises(bc.BroadcastError):
            bc.parse_days(bad)
    assert bc.parse_time("9:05") == "09:05"
    for bad in ("25:00", "10-00", "10:60", ""):
        with pytest.raises(bc.BroadcastError):
            bc.parse_time(bad)
    with pytest.raises(bc.BroadcastError):
        bc.check_tz("Mars/Base")


def test_is_due_window_and_timezone():
    base = {"tz": "Asia/Tashkent", "weekdays": "1234567", "send_time": "10:00", "last_run_date": None,
            "created_at": to_db(at(9) - timedelta(days=2))}
    assert bc.is_due(base, at(9, 59)) == (False, False)
    assert bc.is_due(base, at(10, 0)) == (True, False)
    assert bc.is_due(base, at(10, 30)) == (True, False)
    assert bc.is_due(base, at(10, 31)) == (False, True)  # окно пропущено
    assert bc.is_due({**base, "weekdays": "2"}, at(10, 5)) == (False, False)  # понедельник не в дни
    assert bc.is_due({**base, "last_run_date": "2026-11-16"}, at(10, 5)) == (False, False)
    # часовой пояс: 10:00 в Ташкенте = 05:00 UTC
    utc_now = datetime(2026, 11, 16, 5, 1, tzinfo=timezone.utc)
    assert bc.is_due(base, utc_now)[0]
    ny = {**base, "tz": "America/New_York"}  # 10:00 по Нью-Йорку = 15:00 UTC
    assert not bc.is_due(ny, utc_now)[0]
    assert bc.is_due(ny, datetime(2026, 11, 16, 15, 5, tzinfo=timezone.utc))[0]
    # созданная уже после времени отправки не считается пропущенной
    late = {**base, "created_at": to_db(at(12))}
    assert bc.is_due(late, at(14)) == (False, False)


# ---------- отправка ----------

async def test_text_broadcast_sent_via_business_once_a_day(env):
    await env.client_wrote()
    sid = await env.schedule()
    await env.b.tick()
    (call,) = sent_to(env.bot, CLIENT)
    assert call.text == "Как дела?" and call.business_connection_id == CONN
    assert await env.status(sid) == "sent"
    await env.b.tick()  # повторно в тот же день не шлём
    assert len(sent_to(env.bot, CLIENT)) == 1
    sch = await env.db.sched_get(env.owner["id"], sid)
    assert sch["last_run_date"] == "2026-11-16" and sch["last_run"]
    # следующий день — снова
    env.now = at(10, 1, day=17)
    await env.client_wrote()
    await env.db.sched_update(env.owner["id"], sid, last_run_date="2026-11-16")
    await env.b.tick()
    assert len(sent_to(env.bot, CLIENT)) == 2
    assert [r["status"] for r in await env.db.blog_list(env.owner["id"])] == ["sent", "sent"]


async def test_ai_mode_generates_text(env):
    await env.client_wrote()
    env.groq.replies.append(json.dumps({"text": "Здравствуйте! Как у вас дела?"}))
    sid = await env.schedule(mode="ai", topic="спроси, как дела")
    await env.b.tick()
    assert sent_to(env.bot, CLIENT)[0].text == "Здравствуйте! Как у вас дела?"
    assert "спроси, как дела" in env.groq.prompts[-1][0]["content"]
    assert await env.status(sid) == "sent"


async def test_ai_unavailable_retries_then_fails(env):
    await env.client_wrote()
    sid = await env.schedule(mode="ai", topic="привет")
    env.groq.replies.append("не json")
    await env.b.tick()
    assert (await env.status(sid)).startswith("retry")
    assert sent_to(env.bot, CLIENT) == []
    env.groq.replies.append(json.dumps({"text": "Привет!"}))
    env.now = at(10, 10)
    await env.client_wrote()
    await env.b.tick()
    assert await env.status(sid) == "sent"


async def test_ai_gives_up_after_window(env):
    await env.client_wrote()
    sid = await env.schedule(mode="ai", topic="привет")
    env.now = at(10, 30)  # последняя минута окна
    env.groq.replies.append("мусор")
    await env.client_wrote()
    await env.b.tick()
    assert (await env.status(sid)).startswith("failed")  # окно закрывается — не ждём вечно


async def test_outside_24h_window_manual_text_to_owner(env):
    await env.client_wrote(ago=timedelta(hours=30))
    sid = await env.schedule()
    await env.b.tick()
    assert sent_to(env.bot, CLIENT) == []  # не пытаемся обойти ограничение
    assert (await env.status(sid)).startswith("manual_needed")
    note = [c for c in sent_to(env.bot, ADMIN_ID) if "Рассылка #" in (c.text or "")]
    assert len(note) == 1 and "24 часов" in note[0].text and "Как дела?" in note[0].text


async def test_telegram_rejects_send_manual_text(env):
    await env.client_wrote()
    sid = await env.schedule()
    orig = env.bot.__call__

    async def failing(method, request_timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == CLIENT:
            raise TelegramBadRequest(method=method, message="BUSINESS_PEER_USAGE_MISSING")
        return await orig(method, request_timeout)

    env.bot.__class__ = type("F", (FakeBot,), {"__call__": lambda self, m, request_timeout=None: failing(m, request_timeout)})
    await env.b.tick()
    status = await env.status(sid)
    assert status.startswith("manual_needed") and "BUSINESS_PEER_USAGE_MISSING" in status
    assert any("Как дела?" in (c.text or "") for c in sent_to(env.bot, ADMIN_ID))


async def test_no_connection_or_never_wrote_is_not_sent(env):
    sid = await env.schedule(chat_id=999)  # ручной ID: человек не писал
    await env.b.tick()
    assert (await env.status(sid)).startswith("skipped")
    assert sent_to(env.bot, 999) == []


async def test_daily_cap_across_schedules(env):
    await env.client_wrote()
    a = await env.schedule(text="первое")
    b = await env.schedule(text="второе")
    await env.b.tick()
    assert len(sent_to(env.bot, CLIENT)) == 1  # максимум одно сообщение в сутки на чат
    assert (await env.status(b)).startswith("skipped")
    assert await env.status(a) == "sent"


async def test_stop_word_disables_and_blocks(env):
    await env.client_wrote()
    sid = await env.schedule()
    await env.dp.feed_update(env.bot, biz_update(CLIENT, "Стоп"))
    sch = await env.db.sched_get(env.owner["id"], sid)
    assert sch["active"] == 0 and "стоп" in sch["last_status"]
    assert await env.db.optout_has(env.owner["id"], CLIENT)
    assert any("не буду писать" in (c.text or "") for c in sent_to(env.bot, CLIENT))
    with pytest.raises(bc.BroadcastError):
        await env.schedule(chat_id=CLIENT)
    # и вручную включить нельзя
    await env.dp.feed_update(env.bot, message_update(ADMIN_ID, f"/bcast on {sid}"))
    assert (await env.db.sched_get(env.owner["id"], sid))["active"] == 0
    await env.b.tick()
    assert [c for c in sent_to(env.bot, CLIENT) if c.text == "Как дела?"] == []


async def test_stop_inside_sentence_is_not_a_stop(env):
    from bot.services import business as biz

    assert biz.is_stop("не пиши!") and biz.is_stop("STOP") and biz.is_stop("Хватит.")
    assert not biz.is_stop("стоп, а сколько стоит ноутбук?")


async def test_bot_channel_via_deep_link_and_stop(env):
    dp, bot = env.dp, env.bot
    await dp.feed_update(bot, message_update(321, f"/start sb_{ADMIN_ID}"))
    assert any("Я Кимо, AI-агент" in t for t in bot.texts())
    assert await env.db.bsub_get(env.owner["id"], 321)
    assert any("Новый подписчик" in (c.text or "") for c in sent_to(bot, ADMIN_ID))
    sid = await bc.create_schedule(env.db, env.owner, 321, "10:00", "1234567", "text", "Доброе утро!", "")
    assert (await env.db.sched_get(env.owner["id"], sid))["channel"] == "bot"
    await env.db._write("UPDATE scheduled_messages SET created_at = ?", (to_db(env.now - timedelta(days=1)),))
    await env.b.tick()
    (call,) = [c for c in sent_to(bot, 321) if c.text == "Доброе утро!"]
    assert call.business_connection_id is None and call.text == "Доброе утро!"  # от имени бота, без Business
    await dp.feed_update(bot, message_update(321, "стоп"))
    assert await env.db.bsub_get(env.owner["id"], 321) is None
    assert (await env.db.sched_get(env.owner["id"], sid))["active"] == 0


async def test_invalid_deep_link(env):
    await env.dp.feed_update(env.bot, message_update(321, "/start sb_999999"))
    assert "недействительна" in env.bot.texts()[-1]
    assert await env.db.bsub_list(env.owner["id"]) == []


async def test_blocked_bot_channel_removes_subscription(env):
    await env.dp.feed_update(env.bot, message_update(321, f"/start sb_{ADMIN_ID}"))
    sid = await bc.create_schedule(env.db, env.owner, 321, "10:00", "1234567", "text", "Привет", "")
    await env.db._write("UPDATE scheduled_messages SET created_at = ?", (to_db(env.now - timedelta(days=1)),))

    class Blocked(FakeBot):
        async def __call__(self, method, request_timeout=None):
            if isinstance(method, SendMessage) and method.chat_id == 321:
                raise TelegramForbiddenError(method=method, message="bot was blocked by the user")
            return await super().__call__(method, request_timeout)

    env.b.bot = Blocked()
    await env.b.tick()
    assert (await env.status(sid)).startswith("failed")
    assert await env.db.bsub_get(env.owner["id"], 321) is None


async def test_retry_after_is_retried_not_final(env):
    await env.client_wrote()
    sid = await env.schedule()

    class Slow(FakeBot):
        async def __call__(self, method, request_timeout=None):
            if isinstance(method, SendMessage) and method.chat_id == CLIENT:
                raise TelegramRetryAfter(method=method, message="flood", retry_after=7)
            return await super().__call__(method, request_timeout)

    env.b.bot = Slow()
    await env.b.tick()
    assert (await env.status(sid)).startswith("retry")
    assert (await env.db.sched_get(env.owner["id"], sid))["last_run_date"] is None  # повторим в следующую минуту


async def test_missed_when_window_passed(env):
    await env.client_wrote()
    sid = await env.schedule()
    env.now = at(12, 0)
    await env.b.tick()
    assert (await env.status(sid)).startswith("missed")
    assert sent_to(env.bot, CLIENT) == []


async def test_weekday_filter(env):
    await env.client_wrote()
    await env.schedule(days="2")  # только вторник, а сегодня понедельник
    await env.b.tick()
    assert sent_to(env.bot, CLIENT) == []


async def test_revoked_owner_blocked(env, db):
    user = await db.ensure_user(555, NOW())
    await grants.grant(db, user, 5, ADMIN_ID)
    await env.dp.feed_update(env.bot, conn_update(owner_tg=555, conn_id="c-555"))
    await db.bchat_touch(user["id"], 777, "К", None, to_db(env.now - timedelta(hours=1)), True)
    sid = await bc.create_schedule(db, user, 777, "10:00", "1234567", "text", "Привет", "")
    await db._write("UPDATE scheduled_messages SET created_at = ?", (to_db(env.now - timedelta(days=1)),))
    await grants.revoke(db, user)
    await env.b.tick()
    assert sent_to(env.bot, 777) == []
    assert (await db.sched_get(user["id"], sid))["last_status"].startswith("blocked")


async def test_isolation_between_owners(env, db):
    user = await db.ensure_user(555, NOW())
    other_sid = await bc.create_schedule(db, user, 777, "10:00", "1234567", "text", "чужая", "")
    assert await db.sched_get(env.owner["id"], other_sid) is None
    assert await db.sched_list(env.owner["id"]) == []
    assert await db.sched_update(env.owner["id"], other_sid, active=0) is False
    assert await db.sched_delete(env.owner["id"], other_sid) is False
    assert (await db.sched_get(user["id"], other_sid))["active"] == 1


async def test_validation_and_limits(env):
    for kwargs in (dict(mode="text", text="", topic=""), dict(mode="ai", text="", topic=""),
                   dict(mode="x", text="a", topic=""), dict(mode="text", text="я" * 1001, topic="")):
        with pytest.raises(bc.BroadcastError):
            await bc.create_schedule(env.db, env.owner, 1, "10:00", "1234567", **kwargs)
    with pytest.raises(bc.BroadcastError):
        await bc.create_schedule(env.db, env.owner, 1, "10:00", "1234567", "text", "a", "", channel="bot")  # не подписан
    for i in range(bc.MAX_SCHEDULES):
        await bc.create_schedule(env.db, env.owner, i + 1, "10:00", "1234567", "text", "a", "")
    with pytest.raises(bc.BroadcastError):
        await bc.create_schedule(env.db, env.owner, 99, "10:00", "1234567", "text", "a", "")


async def test_bcast_commands(env):
    dp, bot = env.dp, env.bot
    await env.client_wrote()
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/bcast add {CLIENT} 10:30 weekdays ai спроси, как дела"))
    assert "создана" in bot.texts()[-1]
    sched = (await env.db.sched_list(env.owner["id"]))[0]
    assert sched["mode"] == "ai" and sched["weekdays"] == "12345" and sched["topic"] == "спроси, как дела"
    await dp.feed_update(bot, message_update(ADMIN_ID, "/bcast"))
    assert "спроси, как дела" in bot.texts()[-1]
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/bcast off {sched['id']}"))
    assert (await env.db.sched_get(env.owner["id"], sched["id"]))["active"] == 0
    await dp.feed_update(bot, message_update(ADMIN_ID, "/bcast chats"))
    assert str(CLIENT) in bot.texts()[-1]
    await dp.feed_update(bot, message_update(ADMIN_ID, "/bcast link"))
    assert f"start=sb_{ADMIN_ID}" in bot.texts()[-1]
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/bcast add 5555 10:00 daily text привет"))
    assert "ещё не видел" in bot.texts()[-1]
    await dp.feed_update(bot, message_update(ADMIN_ID, f"/bcast del {sched['id']}"))
    assert await env.db.sched_get(env.owner["id"], sched["id"]) is None
    await dp.feed_update(bot, message_update(ADMIN_ID, "/bcast add 1 25:00 daily text x"))
    assert "ЧЧ:ММ" in bot.texts()[-1]


async def test_user_without_broadcast_perm_cannot_use_commands(env, db):
    from bot.services import perms

    user = await db.ensure_user(555, NOW())
    await grants.grant(db, user, 5, ADMIN_ID)
    await perms.set_perm(db, user, "broadcasts", False)
    await env.dp.feed_update(env.bot, message_update(555, "/bcast add 1 10:00 daily text x"))
    assert "недоступна" in env.bot.texts()[-1]
    assert await db.sched_list(user["id"]) == []


async def test_regular_text_still_reaches_assistant(env):
    """Обработчик «стоп» подписчика не перехватывает обычные сообщения."""
    env.groq.replies.append(json.dumps({"actions": [{"type": "reply", "text": "Привет!"}]}))
    await env.dp.feed_update(env.bot, message_update(ADMIN_ID, "привет"))
    assert any("Привет!" in t for t in env.bot.texts())
