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


async def test_expense_flow(app, db, groq):
    from bot.keyboards import ExpCb
    from tests.fakebot import callback_update

    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, "/spend такси 24000"))
    groq.replies.append(json.dumps({"actions": [{"type": "add_expense", "amount": 3, "currency": "USD", "category": "кофе"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "кофе за 3 доллара"))
    await dp.feed_update(bot, callback_update(ADMIN_ID, ExpCb(action="sum", period="day").pack()))
    summary = bot.texts()[-1]
    assert "60 000 сум" in summary and "$5.00" in summary, summary


async def test_stats_buttons(app, db):
    from bot.keyboards import StatsCb
    from tests.fakebot import callback_update

    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, "/spend еда 50000"))
    await dp.feed_update(bot, message_update(ADMIN_ID, "/add спорт"))
    user = await db.get_user_by_tg(ADMIN_ID)
    (task,) = await db.open_tasks(user["id"])
    await db.complete_task(user["id"], task["id"], task["due_at"])
    for action in ("exp_days", "exp_cat", "tasks_days", "tracker"):
        await dp.feed_update(bot, callback_update(ADMIN_ID, StatsCb(action=action).pack()))
    assert bot.methods().count("SendPhoto") == 3
    assert any("Серия" in t for t in bot.texts())


async def test_paywall_and_stars_payment(app, db, groq):
    from aiogram.types import PreCheckoutQuery, SuccessfulPayment, Update

    from bot.keyboards import PayCb
    from bot.services.subscription import Payload
    from tests.fakebot import callback_update, make_user

    dp, bot, settings = app
    uid = 777
    await dp.feed_update(bot, message_update(uid, "/start"))
    await dp.feed_update(bot, message_update(uid, "купить хлеб"))
    assert "подписка" in bot.texts()[-1] and not groq.prompts  # ИИ не вызывался

    await dp.feed_update(bot, callback_update(uid, PayCb(method="stars").pack()))
    invoice = next(c for c in bot.calls if type(c).__name__ == "SendInvoice")
    assert invoice.currency == "XTR" and invoice.prices[0].amount == 250 and invoice.provider_token == ""

    payload = Payload("stars", uid, 30).pack()
    pcq = PreCheckoutQuery(id="q1", from_user=make_user(uid), currency="XTR", total_amount=250, invoice_payload=payload)
    await dp.feed_update(bot, Update(update_id=9001, pre_checkout_query=pcq))
    answer = next(c for c in bot.calls if type(c).__name__ == "AnswerPreCheckoutQuery")
    assert answer.ok is True

    sp = SuccessfulPayment(
        currency="XTR", total_amount=250, invoice_payload=payload,
        telegram_payment_charge_id="ch_1", provider_payment_charge_id="",
    )
    upd = message_update(uid, None, successful_payment=sp)
    await dp.feed_update(bot, upd)
    await dp.feed_update(bot, upd)  # повтор апдейта не продлевает дважды
    user = await db.get_user_by_tg(uid)
    assert user["sub_until"] is not None and (await db.payments_summary())["n"] == 1

    groq.replies.append('{"actions":[{"type":"reply","text":"ok"}]}')
    await dp.feed_update(bot, message_update(uid, "привет"))
    assert groq.prompts  # теперь доступ есть


async def test_bad_pre_checkout_rejected(app):
    from aiogram.types import PreCheckoutQuery, Update

    from bot.services.subscription import Payload
    from tests.fakebot import make_user

    dp, bot, _ = app
    payload = Payload("stars", 778, 30).pack()
    pcq = PreCheckoutQuery(id="q2", from_user=make_user(778), currency="XTR", total_amount=1, invoice_payload=payload)
    await dp.feed_update(bot, Update(update_id=9002, pre_checkout_query=pcq))
    answer = next(c for c in bot.calls if type(c).__name__ == "AnswerPreCheckoutQuery")
    assert answer.ok is False


async def test_admin_commands(app, db):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(ADMIN_ID, "/grant 4242 10"))
    user = await db.get_user_by_tg(4242)
    assert user and user["sub_until"]
    await dp.feed_update(bot, message_update(ADMIN_ID, "/users"))
    assert "4242" in bot.texts()[-1]
    await dp.feed_update(bot, message_update(ADMIN_ID, "/stats"))
    assert "Пользователей" in bot.texts()[-1]
    await dp.feed_update(bot, message_update(ADMIN_ID, "/revoke 4242"))
    assert (await db.get_user_by_tg(4242))["sub_until"] is None
    await dp.feed_update(bot, message_update(ADMIN_ID, "/setsecret click_provider_token 1:TEST:x"))
    assert "DeleteMessage" in bot.methods()

    # не-админ не может выдать себе доступ
    await dp.feed_update(bot, message_update(4242, "/grant 4242 999"))
    assert (await db.get_user_by_tg(4242))["sub_until"] is None


async def test_subscription_reminder(app, db):
    from datetime import timedelta

    from bot.services.scheduler import BotScheduler
    from bot.timeutil import now_utc, to_db

    dp, bot, settings = app
    user = await db.ensure_user(31337, to_db(now_utc()))
    await db.set_sub_until(user["id"], to_db(now_utc() + timedelta(days=2)))
    sched = BotScheduler(bot, db, settings)
    await sched.subscription_reminders()
    await sched.subscription_reminders()
    assert sum("Подписка заканчивается" in t for t in bot.texts()) == 1
