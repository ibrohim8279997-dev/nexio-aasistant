"""Этап 2: ответы клиентам через Telegram Business."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Chat, Message, Update, User, Voice

from bot.handlers import business as hb
from bot.services import business as biz
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID
from tests.fakebot import _next_id, message_update

CONN = "conn-1"
CLIENT = 777
OWNER = ADMIN_ID


@pytest.fixture(autouse=True)
def _reset():
    hb.reset_state()
    yield
    hb.reset_state()


def reply_json(text="Ответ", handoff=False, reason=""):
    return json.dumps({"reply": text, "handoff": handoff, "reason": reason}, ensure_ascii=False)


def conn_update(owner_tg=OWNER, can_reply=True, enabled=True, conn_id=CONN):
    from aiogram.types import BusinessBotRights

    c = BusinessConnection(
        id=conn_id, user=User(id=owner_tg, is_bot=False, first_name="Тони"), user_chat_id=owner_tg,
        date=datetime.now(timezone.utc), is_enabled=enabled, rights=BusinessBotRights(can_reply=can_reply),
    )
    return Update(update_id=_next_id(), business_connection=c)


def biz_update(sender, text="Привет", chat_id=CLIENT, conn_id=CONN, chat_type="private", is_bot=False,
               message_id=None, **extra):
    msg = Message(
        message_id=message_id or _next_id(), date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type=chat_type, first_name="Клиент"),
        from_user=User(id=sender, is_bot=is_bot, first_name="Клиент"), text=text, business_connection_id=conn_id, **extra,
    )
    return Update(update_id=_next_id(), business_message=msg)


def sent_to(bot, chat_id):
    return [c for c in bot.calls if type(c).__name__ == "SendMessage" and c.chat_id == chat_id]


async def connect(app, **kw):
    dp, bot, _ = app
    await dp.feed_update(bot, conn_update(**kw))


async def test_business_updates_are_subscribed(app):
    dp, _, _ = app
    used = set(dp.resolve_used_update_types())
    assert {"business_connection", "business_message", "edited_business_message", "deleted_business_messages"} <= used


async def test_connection_saved_and_owner_notified(app, db):
    dp, bot, _ = app
    await connect(app)
    owner = await db.get_user_by_tg(OWNER)
    conn = await db.bconn_get(CONN)
    assert conn["owner_id"] == owner["id"] and conn["can_reply"] == 1 and conn["is_enabled"] == 1
    assert any("Business подключён" in t for t in bot.texts())


async def test_connection_without_reply_right_warns(app, db):
    dp, bot, _ = app
    await connect(app, can_reply=False)
    assert (await db.bconn_get(CONN))["can_reply"] == 0
    assert any("без права отвечать" in t for t in bot.texts())
    await dp.feed_update(bot, biz_update(CLIENT))
    assert sent_to(bot, CLIENT) == []  # без права отвечать молчим


async def test_first_message_introduces_and_uses_business_connection(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("Цены на ноутбуки от 5 млн сум."))
    await dp.feed_update(bot, biz_update(CLIENT, "Сколько стоят ноутбуки?"))
    (call,) = sent_to(bot, CLIENT)
    assert call.business_connection_id == CONN
    assert call.text.startswith("Здравствуйте! Я Кимо, AI-агент Тони.")
    assert "Цены на ноутбуки" in call.text
    # второе сообщение — без представления
    groq.replies.append(reply_json("Есть в наличии."))
    await dp.feed_update(bot, biz_update(CLIENT, "А в наличии?"))
    second = sent_to(bot, CLIENT)[1]
    assert "Я Кимо" not in second.text
    owner = await db.get_user_by_tg(OWNER)
    roles = [m["role"] for m in await db.bmsg_recent(owner["id"], CLIENT)]
    assert roles == ["client", "assistant", "client", "assistant"]


async def test_uzbek_intro(app, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("Narxi 5 mln so'm."))
    await dp.feed_update(bot, biz_update(CLIENT, "Salom, noutbuk narxi qancha?"))
    assert sent_to(bot, CLIENT)[0].text.startswith("Assalomu alaykum! Men Kimo")


async def test_prompt_contains_rules_and_context(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    owner = await db.get_user_by_tg(OWNER)
    await db.brules_set(owner["id"], to_db(now_utc()), prohibitions="Не называть скидки", business_info="MilliyPC: ПК")
    for i in range(3):
        groq.replies.append(reply_json(f"ответ {i}"))
        await dp.feed_update(bot, biz_update(CLIENT, f"вопрос {i}"))
    system = groq.prompts[-1][0]["content"]
    assert "Не называть скидки" in system and "MilliyPC: ПК" in system and "AI-агент Тони" in system
    assert "никогда не выдавай себя за человека" in system.lower()
    assert [m["content"] for m in groq.prompts[-1][1:]][0].startswith("вопрос 0")  # контекст диалога


async def test_context_limited_to_20(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    owner = await db.get_user_by_tg(OWNER)
    for i in range(30):
        await db.bmsg_add(owner["id"], CLIENT, "client", f"старое {i}", i + 1, to_db(now_utc()))
    groq.replies.append(reply_json("ок"))
    await dp.feed_update(bot, biz_update(CLIENT, "новое"))
    assert len(groq.prompts[-1]) == 1 + biz.CONTEXT_MESSAGES


async def test_ai_question_answered_honestly_without_model(app, groq):
    dp, bot, _ = app
    await connect(app)
    await dp.feed_update(bot, biz_update(CLIENT, "Вы человек или бот?"))  # replies пуст: модель не вызывается
    (call,) = sent_to(bot, CLIENT)
    assert "AI-агент" in call.text and "не человек" in call.text


async def test_client_asks_human_notifies_owner_and_pauses(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    await dp.feed_update(bot, biz_update(CLIENT, "Позовите менеджера пожалуйста"))
    (call,) = sent_to(bot, CLIENT)
    assert "владельцу" in call.text
    notes = [c for c in sent_to(bot, OWNER) if "Нужен ответ владельца" in (c.text or "")]
    assert len(notes) == 1 and f"tg://user?id={CLIENT}" in notes[0].text
    owner = await db.get_user_by_tg(OWNER)
    assert biz.is_paused(await db.bchat_get(owner["id"], CLIENT))
    await dp.feed_update(bot, biz_update(CLIENT, "Алло?"))  # на паузе — молчим
    assert len(sent_to(bot, CLIENT)) == 1


async def test_out_of_rules_handoff_from_model(app, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("Уточню у владельца, он свяжется с вами.", handoff=True, reason="нет данных о гарантии"))
    await dp.feed_update(bot, biz_update(CLIENT, "Какая гарантия на видеокарту X?"))
    assert any("нет данных о гарантии" in (c.text or "") for c in sent_to(bot, OWNER))


async def test_owner_manual_reply_pauses_ai(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("Здравствуйте"))
    await dp.feed_update(bot, biz_update(CLIENT, "Привет"))
    n = len(sent_to(bot, CLIENT))
    await dp.feed_update(bot, biz_update(OWNER, "Сейчас отвечу лично", chat_id=CLIENT))
    owner = await db.get_user_by_tg(OWNER)
    assert biz.is_paused(await db.bchat_get(owner["id"], CLIENT))
    await dp.feed_update(bot, biz_update(CLIENT, "Ещё вопрос"))
    assert len(sent_to(bot, CLIENT)) == n  # не отвечаем клиенту и не отвечаем владельцу
    assert groq.replies == []


async def test_own_bot_message_does_not_pause(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("Здравствуйте"))
    await dp.feed_update(bot, biz_update(CLIENT, "Привет"))
    owner = await db.get_user_by_tg(OWNER)
    ours = (await db.bmsg_recent(owner["id"], CLIENT))[-1]
    # апдейт про сообщение, которое отправил сам бот от имени владельца
    echo = biz_update(OWNER, "эхо нашего сообщения", chat_id=CLIENT, message_id=ours["tg_msg_id"])
    await dp.feed_update(bot, echo)
    assert not biz.is_paused(await db.bchat_get(owner["id"], CLIENT))


async def test_pause_hours_setting_and_expiry(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    owner = await db.get_user_by_tg(OWNER)
    await db.brules_set(owner["id"], to_db(now_utc()), pause_hours=1)
    await db.bchat_touch(owner["id"], CLIENT, "К", None, to_db(now_utc()), True)
    await db.bchat_set(owner["id"], CLIENT, paused_until=to_db(now_utc() - timedelta(minutes=1)))
    groq.replies.append(reply_json("ок"))
    await dp.feed_update(bot, biz_update(CLIENT, "Привет"))
    assert len(sent_to(bot, CLIENT)) == 1  # пауза истекла


async def test_ignored_senders_and_chats(app, groq):
    dp, bot, _ = app
    await connect(app)
    await dp.feed_update(bot, biz_update(CLIENT, "я бот", is_bot=True))
    await dp.feed_update(bot, biz_update(CLIENT, "в группе", chat_id=-100, chat_type="supergroup"))
    await dp.feed_update(bot, biz_update(CLIENT, "в канале", chat_id=-200, chat_type="channel"))
    await dp.feed_update(bot, biz_update(CLIENT, "чужое подключение", conn_id="unknown"))
    assert [c for c in bot.calls if type(c).__name__ == "SendMessage" and c.chat_id in (CLIENT, -100, -200)] == []


async def test_flood_limit(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    owner = await db.get_user_by_tg(OWNER)
    await db.brules_set(owner["id"], to_db(now_utc()), flood_per_min=2)
    for _ in range(4):
        groq.replies.append(reply_json("ок"))
        await dp.feed_update(bot, biz_update(CLIENT, "привет"))
    assert len(sent_to(bot, CLIENT)) == 2


def test_flood_guard_window():
    t = [0.0]
    g = biz.FloodGuard(clock=lambda: t[0])
    assert g.allow(1, 1, 2) and g.allow(1, 1, 2) and not g.allow(1, 1, 2)
    assert g.allow(1, 2, 2)  # другой чат
    t[0] = 61
    assert g.allow(1, 1, 2)
    assert not g.allow(1, 1, 0)


async def test_excluded_chat_and_global_pause(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("ок"))
    await dp.feed_update(bot, biz_update(CLIENT, "привет"))
    await dp.feed_update(bot, message_update(OWNER, f"/pause {CLIENT}"))
    await dp.feed_update(bot, biz_update(CLIENT, "ещё"))
    assert len(sent_to(bot, CLIENT)) == 1
    await dp.feed_update(bot, message_update(OWNER, f"/resume {CLIENT}"))
    groq.replies.append(reply_json("снова"))
    await dp.feed_update(bot, biz_update(CLIENT, "а теперь?"))
    assert len(sent_to(bot, CLIENT)) == 2
    await dp.feed_update(bot, message_update(OWNER, "/pause"))
    await dp.feed_update(bot, biz_update(CLIENT, "алло"))
    assert len(sent_to(bot, CLIENT)) == 2
    await dp.feed_update(bot, message_update(OWNER, "/resume"))
    groq.replies.append(reply_json("вернулся"))
    await dp.feed_update(bot, biz_update(CLIENT, "алло"))
    assert len(sent_to(bot, CLIENT)) == 3


async def test_user_without_access_gets_no_ai_replies(app, db, groq):
    dp, bot, _ = app
    await connect(app, owner_tg=555, conn_id="c-555")
    assert any("доступа к Кимо у вас нет" in t for t in bot.texts())
    await dp.feed_update(bot, biz_update(CLIENT, "привет", conn_id="c-555"))
    assert sent_to(bot, CLIENT) == []


async def test_user_with_grant_replies_and_data_is_isolated(app, db, groq):
    from bot.services import grants

    dp, bot, _ = app
    user = await db.ensure_user(555, to_db(now_utc()))
    await grants.grant(db, user, 5, ADMIN_ID)
    await connect(app)
    await connect(app, owner_tg=555, conn_id="c-555")
    groq.replies += [reply_json("от тони"), reply_json("от пользователя")]
    await dp.feed_update(bot, biz_update(CLIENT, "привет"))
    await dp.feed_update(bot, biz_update(888, "привет", chat_id=888, conn_id="c-555"))
    owner, other = await db.get_user_by_tg(OWNER), await db.get_user_by_tg(555)
    assert [c["chat_id"] for c in await db.bchat_list(owner["id"])] == [CLIENT]
    assert [c["chat_id"] for c in await db.bchat_list(other["id"])] == [888]
    assert await db.bmsg_recent(owner["id"], 888) == []
    assert sent_to(bot, 888)[0].business_connection_id == "c-555"
    # правила изолированы
    await db.brules_set(other["id"], to_db(now_utc()), prohibitions="только у второго")
    assert (await db.brules_get(owner["id"])) is None


async def test_user_without_business_perm_is_silent(app, db, groq):
    from bot.services import perms

    dp, bot, _ = app
    await connect(app)
    owner = await db.get_user_by_tg(OWNER)
    user = await db.ensure_user(555, to_db(now_utc()))
    await db.set_user_setting(user["id"], "x", "1")
    from bot.services import grants

    await grants.grant(db, user, 5, ADMIN_ID)
    await perms.set_perm(db, user, "business", False)
    await connect(app, owner_tg=555, conn_id="c-555")
    await dp.feed_update(bot, biz_update(CLIENT, "привет", conn_id="c-555"))
    assert sent_to(bot, CLIENT) == []
    await dp.feed_update(bot, message_update(555, "/brules"))
    assert any("недоступна" in t for t in bot.texts())
    assert owner  # владелец не затронут


async def test_model_failure_on_first_message_still_introduces(app, groq):
    from bot.ai.groq_client import GroqUnavailableError

    dp, bot, _ = app
    await connect(app)

    async def boom(messages, max_tokens=2048):
        raise GroqUnavailableError("x")

    groq.chat_json = boom
    await dp.feed_update(bot, biz_update(CLIENT, "Сколько стоит?"))
    assert "Я Кимо" in sent_to(bot, CLIENT)[0].text
    assert any("не смог ответить" in (c.text or "") for c in sent_to(bot, OWNER))


async def test_bad_model_json_is_silent(app, groq):
    dp, bot, _ = app
    await connect(app)
    await dp.feed_update(bot, biz_update(CLIENT, "привет"))  # первое сообщение, но…
    groq.replies.append("не json")
    await dp.feed_update(bot, biz_update(CLIENT, "ещё"))
    assert len(sent_to(bot, CLIENT)) <= 1  # второй ответ не придуман


async def test_edit_and_delete_update_journal(app, db, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("ок"))
    upd = biz_update(CLIENT, "оригинал")
    await dp.feed_update(bot, upd)
    owner = await db.get_user_by_tg(OWNER)
    mid = upd.business_message.message_id
    edit = Message(message_id=mid, date=datetime.now(timezone.utc), chat=Chat(id=CLIENT, type="private"),
                   from_user=User(id=CLIENT, is_bot=False, first_name="К"), text="исправлено", business_connection_id=CONN)
    await dp.feed_update(bot, Update(update_id=_next_id(), edited_business_message=edit))
    assert any(m["text"] == "исправлено" for m in await db.bmsg_recent(owner["id"], CLIENT))
    deleted = BusinessMessagesDeleted(business_connection_id=CONN, chat=Chat(id=CLIENT, type="private"), message_ids=[mid])
    await dp.feed_update(bot, Update(update_id=_next_id(), deleted_business_messages=deleted))
    assert not any(m["tg_msg_id"] == mid for m in await db.bmsg_recent(owner["id"], CLIENT))


async def test_cleanup_older_than_30_days(app, db):
    owner = await db.ensure_user(OWNER, to_db(now_utc()))
    await db.bmsg_add(owner["id"], 1, "client", "старое", 1, to_db(now_utc() - timedelta(days=31)))
    await db.bmsg_add(owner["id"], 1, "client", "свежее", 2, to_db(now_utc()))
    assert await db.bmsg_cleanup(biz.cutoff()) == 1
    assert [m["text"] for m in await db.bmsg_recent(owner["id"], 1)] == ["свежее"]


async def test_voice_is_transcribed(app, groq):
    dp, bot, _ = app
    groq.transcript = "Сколько стоит монитор"

    class B:
        async def download(self, media, destination):
            destination.write(b"ogg")

    msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=CLIENT, type="private"),
                  voice=Voice(file_id="f", file_unique_id="u", duration=5, file_size=1000))
    assert await hb._client_text(msg, B(), groq, 1) == "Сколько стоит монитор"
    long = Message(message_id=2, date=datetime.now(timezone.utc), chat=Chat(id=CLIENT, type="private"),
                   voice=Voice(file_id="f", file_unique_id="u", duration=999, file_size=1000))
    assert await hb._client_text(long, B(), groq, 1) is None


async def test_brules_command_roundtrip(app, db):
    dp, bot, _ = app
    await dp.feed_update(bot, message_update(OWNER, "/brules prompt Скидок не давать"))
    await dp.feed_update(bot, message_update(OWNER, "/brules name Тони"))
    await dp.feed_update(bot, message_update(OWNER, "/brules pause 3"))
    await dp.feed_update(bot, message_update(OWNER, "/brules pause abc"))
    owner = await db.get_user_by_tg(OWNER)
    r = await db.brules_get(owner["id"])
    assert r["system_prompt"] == "Скидок не давать" and r["owner_name"] == "Тони" and r["pause_hours"] == 3
    assert any("Нужно число" in t for t in bot.texts())
    with pytest.raises(ValueError):
        await db.brules_set(owner["id"], to_db(now_utc()), evil="x")


async def test_no_gifts_or_payments_methods_used(app, groq):
    dp, bot, _ = app
    await connect(app)
    groq.replies.append(reply_json("ок"))
    await dp.feed_update(bot, biz_update(CLIENT, "привет"))
    used = {type(c).__name__ for c in bot.calls}
    assert not any(n for n in used if "Gift" in n or "Invoice" in n or "Star" in n)
