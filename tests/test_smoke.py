"""Прогон апдейтов через диспетчер без сети."""
import json

from tests.conftest import ADMIN_ID
from tests.fakebot import message_update


async def test_free_text_creates_task(app, db, groq):
    dp, bot, _ = app
    groq.replies.append(json.dumps({"actions": [{"type": "add_task", "text": "Купить хлеб", "date": None, "time": "23:59"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "в 23:59 купить хлеб"))
    assert any("Задача добавлена" in t for t in bot.texts()), bot.texts()
    user = await db.get_user_by_tg(ADMIN_ID)
    tasks = await db.open_tasks(user["id"])
    assert len(tasks) == 1 and tasks[0]["text"] == "Купить хлеб" and tasks[0]["has_time"] == 1


async def test_bad_model_output_is_handled(app, groq):
    dp, bot, _ = app
    groq.replies.append("это не json")
    await dp.feed_update(bot, message_update(ADMIN_ID, "абракадабра"))
    assert any("Не понял" in t for t in bot.texts())


async def test_unknown_command(app):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, "/nope"))
    assert any("Неизвестная команда" in t for t in bot.texts())


async def test_task_buttons_flow(app, db):
    from bot.keyboards import TaskCb
    from tests.fakebot import callback_update

    dp, bot, settings = app
    await dp.feed_update(bot, message_update(ADMIN_ID, "/add завтра 10:00 спортзал"))
    assert any("Задача добавлена" in t for t in bot.texts()), bot.texts()
    user = await db.get_user_by_tg(ADMIN_ID)
    (task,) = await db.open_tasks(user["id"])

    await dp.feed_update(bot, callback_update(ADMIN_ID, TaskCb(action="list", day="tomorrow").pack()))
    assert any("спортзал" in t for t in bot.texts())
    await dp.feed_update(bot, callback_update(ADMIN_ID, TaskCb(action="mvtom", task_id=task["id"], day="tomorrow").pack()))
    await dp.feed_update(bot, callback_update(ADMIN_ID, TaskCb(action="done", task_id=task["id"], day="today").pack()))
    assert (await db.get_task(user["id"], task["id"]))["done_at"] is not None

    # чужой пользователь не может удалить задачу
    await dp.feed_update(bot, callback_update(555, TaskCb(action="del", task_id=task["id"], day="today").pack()))
    assert await db.get_task(user["id"], task["id"]) is not None


async def test_reminder_sent_once(app, db):
    from bot.services.scheduler import BotScheduler
    from bot.timeutil import now_utc, to_db

    dp, bot, settings = app
    user = await db.get_or_create_user(ADMIN_ID, None, "A", to_db(now_utc()))
    await db.add_task(user["id"], "выпить воды", to_db(now_utc()), True, to_db(now_utc()))
    sched = BotScheduler(bot, db, settings)
    await sched.task_reminders()
    await sched.task_reminders()
    assert sum("выпить воды" in t for t in bot.texts()) == 1
