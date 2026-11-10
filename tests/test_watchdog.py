"""Heartbeat, живость агентов, быстрый таймаут заданий с понятной причиной."""
import json
import time
from datetime import timedelta

from bot.agents.bus import Bus
from bot.agents.health import AgentState, Heartbeat, effective_status, reason_text
from bot.agents.registry import Registry
from bot.agents.watchdog import Watchdog
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID
from tests.fakebot import group_message_update

NOW = "2026-11-10 10:00:00"


async def setup_midi(db, enabled=True):
    reg = Registry(db)
    await reg.seed_defaults()
    await db.insert_agent(
        {"name": "midi", "display_name": "Миди", "role": "агент встреч", "enabled": enabled,
         "created_by": "agent_builder", "config": {"kind": "generic"}}, NOW,
    )
    return reg


def age_task(db, task_id, seconds, column="created_at"):
    ts = to_db(now_utc() - timedelta(seconds=seconds))
    return db._write(f"UPDATE tasks_queue SET {column} = ? WHERE id = ?", (ts, task_id))


class Notices:
    def __init__(self):
        self.items = []

    async def __call__(self, text, target=None):
        self.items.append(text)


def test_effective_status_offline_after_90s():
    now = now_utc()
    fresh, old = to_db(now - timedelta(seconds=30)), to_db(now - timedelta(seconds=120))
    assert effective_status(fresh, "online", now) == "online"
    assert effective_status(fresh, "busy", now) == "busy"
    assert effective_status(old, "online", now) == "offline"
    assert effective_status(None, "online", now) == "offline"
    assert effective_status(fresh, "offline", now) == "offline"  # штатная остановка


async def test_heartbeat_writes_status_and_error_without_secrets(db):
    await setup_midi(db)
    state = AgentState()
    state.set_error("Groq 401 для ключа gsk_" + "a" * 20 + " и токена 7123456789:AAH" + "x" * 32)
    hb = Heartbeat(db, "midi", state, llm=type("L", (), {"rate_limit_until": 0.0})())
    await hb.beat()
    row = await db.get_agent("midi")
    assert row["status"] == "error" and row["last_seen"]
    assert "gsk_" not in row["last_error"] and "AAH" not in row["last_error"] and "***" in row["last_error"]

    state.last_error_at = time.time() - 600  # ошибка давняя — снова online, но текст остаётся для диагностики
    await hb.beat()
    row = await db.get_agent("midi")
    assert row["status"] == "online" and row["last_error"]

    state.busy_tasks = 1
    await hb.beat()
    assert (await db.get_agent("midi"))["status"] == "busy"

    hb.llm = type("L", (), {"rate_limit_until": time.time() + 30})()  # 429 от Groq
    await hb.beat()
    row = await db.get_agent("midi")
    assert row["status"] == "busy" and row["status_detail"]["reason"] == "rate_limit" and row["status_detail"]["until"]
    assert "лимит Groq" in reason_text("Миди", row["last_seen"], row["status"], row["last_error"], row["status_detail"])


async def test_offline_agent_task_failed_fast_and_owner_told_why(db):
    reg = await setup_midi(db)  # у Миди нет heartbeat -> offline
    bus = Bus(db, "kimo", owner_id=ADMIN_ID)
    task_id = await bus.send("midi", {"type": "ask", "text": "назначь встречу на пятницу"}, chat_id=-100, reply_to=5)
    await age_task(db, task_id, 12)  # прошло 12 с, а не 15-20 минут
    notices = Notices()
    assert await Watchdog(db, reg, notices, own_agent="kimo").check() == [task_id]
    task = await db.queue_get(task_id)
    assert task["status"] == "failed" and "Миди offline" in task["result"] and task["delivered"] == 1
    assert "Миди offline" in notices.items[0] and "назначь встречу" in notices.items[0]


async def test_fresh_offline_gets_grace_and_internal_pings_are_silent(db):
    reg = await setup_midi(db)
    bus = Bus(db, "kimo", owner_id=ADMIN_ID)
    new_task = await bus.send("midi", {"type": "ask", "text": "x"})
    ping = await bus.send("midi", {"type": "ping"})
    notices = Notices()
    wd = Watchdog(db, reg, notices, own_agent="kimo")
    assert await wd.check() == []  # агента только что вызвали: 10 с на запуск
    await age_task(db, new_task, 20)
    await age_task(db, ping, 20)
    assert set(await wd.check()) == {new_task, ping}
    assert len(notices.items) == 1  # про служебный ping владельцу не пишем


async def test_busy_rate_limit_reason(db):
    reg = await setup_midi(db)
    until = time.time() + 45
    state = AgentState()
    await Heartbeat(db, "midi", state, llm=type("L", (), {"rate_limit_until": until})()).beat()
    task_id = await Bus(db, "kimo", owner_id=ADMIN_ID).send("midi", {"type": "ask", "text": "x"})
    await age_task(db, task_id, 70)
    notices = Notices()
    await Watchdog(db, reg, notices, own_agent="kimo").check()
    assert "лимит Groq" in (await db.queue_get(task_id))["result"] and "лимит Groq" in notices.items[0]


async def test_timeout_is_configurable_per_agent(db):
    reg = await setup_midi(db)
    await db.update_agent("midi", config={"kind": "generic", "task_timeout_s": 180})
    await Heartbeat(db, "midi", AgentState()).beat()  # агент жив и свободен
    task_id = await Bus(db, "kimo", owner_id=ADMIN_ID).send("midi", {"type": "ask", "text": "найди в интернете"})
    wd = Watchdog(db, reg, Notices(), own_agent="kimo")
    await age_task(db, task_id, 100)
    assert await wd.check() == []  # долгая задача (поиск): 60 с мало, но у этого агента лимит 180
    await age_task(db, task_id, 200)
    assert await wd.check() == [task_id]
    assert "не взял задание за 180 с" in (await db.queue_get(task_id))["result"]


async def test_taken_task_of_dead_agent_fails(db):
    reg = await setup_midi(db)
    await db._write("UPDATE agents SET last_seen = ?, status = 'online' WHERE name = 'midi'", (to_db(now_utc() - timedelta(seconds=200)),))
    task_id = await Bus(db, "kimo", owner_id=ADMIN_ID).send("midi", {"type": "ask", "text": "x"})
    assert (await Bus(db, "midi").claim())["id"] == task_id
    await age_task(db, task_id, 30, column="updated_at")
    notices = Notices()
    assert await Watchdog(db, reg, notices, own_agent="kimo").check() == [task_id]
    assert "перестал отвечать" in (await db.queue_get(task_id))["result"]


async def test_late_result_of_closed_task_is_dropped(db):
    await setup_midi(db)
    bus = Bus(db, "kimo", owner_id=ADMIN_ID)
    task_id = await bus.send("midi", {"type": "ask", "text": "x"})
    midi = Bus(db, "midi")
    await midi.claim()
    await db.queue_force_fail(task_id, "⚠️ таймаут", to_db(now_utc()))
    assert await midi.finish(task_id, "поздний ответ") is False
    assert (await db.queue_get(task_id))["result"] == "⚠️ таймаут"


async def test_kimo_does_not_delegate_to_offline_agent(app, db, groq):
    dp, bot, _ = app
    await db._write("UPDATE agents SET last_seen = ?, status = 'online' WHERE name = 'assistant'",
                    (to_db(now_utc() - timedelta(seconds=300)),))
    groq.replies.append(json.dumps({"actions": [{"type": "delegate", "agent": "Мими", "task": "найди рецепт"}]}))
    await dp.feed_update(bot, group_message_update(ADMIN_ID, "передай Мими: найди рецепт плова"))
    assert "Мими offline" in bot.texts()[-1] and "не передано" in bot.texts()[-1]
    assert await db.queue_claim("assistant", NOW) is None  # задание не создано
