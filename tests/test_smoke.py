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
