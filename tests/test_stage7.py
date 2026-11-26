"""Этап 7: голосовые/текстовые команды (рассылка, пауза Business, открыть Mini App), изоляция по всем таблицам, шина."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from bot.ai.actions import CreateBroadcast, OpenApp, PauseBusiness, parse_actions
from bot.services import assistant, broadcast as bc, grants, perms
from bot.timeutil import now_local, now_utc, to_db
from tests.conftest import ADMIN_ID, FakeRates
from tests.fakebot import message_update

TODAY = now_local().date()
NOW = lambda: to_db(now_utc())  # noqa: E731


def acts(*items):
    return parse_actions(json.dumps({"actions": list(items)}), TODAY)


async def owner_with_chats(db, tg=ADMIN_ID):
    owner = await db.ensure_user(tg, NOW())
    await db.bchat_touch(owner["id"], 777, "Азиз Каримов", "aziz", NOW(), True)
    await db.bchat_touch(owner["id"], 778, "Азиза Юсупова", None, NOW(), True)
    await db.bchat_touch(owner["id"], 779, "Дилноза", None, NOW(), True)
    await db.bconn_upsert("c1", owner["id"], tg, True, True, NOW())
    return owner


def deps_for(db, owner, **kw):
    return assistant.AssistantDeps(db=db, user=owner, rates=FakeRates(db), **kw)


def test_new_actions_parse_and_validate():
    a, b, c = acts({"type": "create_broadcast", "chat": "Дилноза", "time": "10:00", "days": "daily", "mode": "ai", "topic": "спроси, как дела"},
                   {"type": "pause_business", "enabled": False}, {"type": "open_app", "section": "stats"})
    assert isinstance(a, CreateBroadcast) and isinstance(b, PauseBusiness) and b.chat is None and isinstance(c, OpenApp)
    assert acts({"type": "open_app", "section": "что-то"})[0].section == "home"  # неизвестный раздел -> главная
    with pytest.raises(Exception):  # некорректное время отбрасывается проверкой
        acts({"type": "create_broadcast", "chat": "x", "time": "утром"})


async def test_create_broadcast_by_name_id_and_ambiguity(db):
    owner = await owner_with_chats(db)
    d = deps_for(db, owner)
    [ok] = await assistant.execute(acts({"type": "create_broadcast", "chat": "дилноз", "time": "10:00", "days": "weekdays", "mode": "ai", "topic": "спроси, как дела"}), d)
    assert "Рассылка #" in ok.text
    sched = (await db.sched_list(owner["id"]))[0]
    assert sched["chat_id"] == 779 and sched["send_time"] == "10:00" and sched["weekdays"] == "12345" and sched["mode"] == "ai"
    [amb] = await assistant.execute(acts({"type": "create_broadcast", "chat": "Ази", "time": "11:00", "mode": "text", "text": "Привет"}), d)
    assert "Уточните" in amb.text and len(await db.sched_list(owner["id"])) == 1
    [none] = await assistant.execute(acts({"type": "create_broadcast", "chat": "Петя", "time": "11:00", "mode": "text", "text": "Привет"}), d)
    assert "Не нашёл" in none.text
    [byid] = await assistant.execute(acts({"type": "create_broadcast", "chat": "777", "time": "12:00", "mode": "text", "text": "Добрый день"}), d)
    assert "Рассылка #" in byid.text
    [bad] = await assistant.execute(acts({"type": "create_broadcast", "chat": "777", "time": "12:00", "mode": "text", "text": ""}), d)
    assert "⚠️" in bad.text  # пустой текст


async def test_broadcast_command_respects_stop_and_perms(db):
    owner = await owner_with_chats(db)
    await db.optout_add(owner["id"], 779, NOW())
    [r] = await assistant.execute(acts({"type": "create_broadcast", "chat": "779", "time": "10:00", "mode": "text", "text": "Привет"}), deps_for(db, owner))
    assert "стоп" in r.text and await db.sched_list(owner["id"]) == []
    user = await db.ensure_user(555, NOW())
    await grants.grant(db, user, 5, ADMIN_ID)
    await perms.set_perm(db, user, "broadcasts", False)
    d = deps_for(db, user, perms=await perms.effective_perms(db, user, ADMIN_ID))
    [r] = await assistant.execute(acts({"type": "create_broadcast", "chat": "1", "time": "10:00", "mode": "text", "text": "x"}), d)
    assert "недоступна" in r.text and await db.sched_list(user["id"]) == []


async def test_pause_business_global_and_chat(db):
    owner = await owner_with_chats(db)
    d = deps_for(db, owner)
    [r] = await assistant.execute(acts({"type": "pause_business", "enabled": False}), d)
    assert "молчит" in r.text and (await db.bconn_get("c1"))["ai_enabled"] == 0
    [r] = await assistant.execute(acts({"type": "pause_business", "enabled": True}), d)
    assert (await db.bconn_get("c1"))["ai_enabled"] == 1
    [r] = await assistant.execute(acts({"type": "pause_business", "enabled": False, "chat": "Дилноза"}), d)
    assert (await db.bchat_get(owner["id"], 779))["excluded"] == 1
    [r] = await assistant.execute(acts({"type": "pause_business", "enabled": True, "chat": "779"}), d)
    assert (await db.bchat_get(owner["id"], 779))["excluded"] == 0
    [r] = await assistant.execute(acts({"type": "pause_business", "enabled": False, "chat": "123456"}), d)
    assert "нет среди чатов" in r.text
    fresh = await db.ensure_user(556, NOW())
    [r] = await assistant.execute(acts({"type": "pause_business", "enabled": False}), deps_for(db, fresh))
    assert "ещё не подключён" in r.text


async def test_open_app_button(db):
    owner = await owner_with_chats(db)
    [no] = await assistant.execute(acts({"type": "open_app", "section": "stats"}), deps_for(db, owner))
    assert "не настроен" in no.text and no.markup is None
    [yes] = await assistant.execute(acts({"type": "open_app", "section": "stats"}), deps_for(db, owner, miniapp_url="https://kimo.example.ts.net"))
    button = yes.markup.inline_keyboard[0][0]
    assert button.web_app.url == "https://kimo.example.ts.net/?s=stats"
    [home] = await assistant.execute(acts({"type": "open_app", "section": "home"}), deps_for(db, owner, miniapp_url="https://kimo.example.ts.net"))
    assert home.markup.inline_keyboard[0][0].web_app.url == "https://kimo.example.ts.net"


async def test_chat_phrase_creates_broadcast_and_prompt_lists_chats(app, db, groq):
    dp, bot, _ = app
    owner = await owner_with_chats(db)
    groq.replies.append(json.dumps({"actions": [{"type": "create_broadcast", "chat": "Дилноза", "time": "10:00", "days": "daily", "mode": "ai", "topic": "спроси, как дела"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "Каждый день в 10:00 спрашивай Дилнозу, как дела"))
    assert any("Рассылка #" in t for t in bot.texts()), bot.texts()
    assert "779: Дилноза" in groq.prompts[-1][0]["content"]  # модель видит список чатов
    assert (await db.sched_list(owner["id"]))[0]["topic"] == "спроси, как дела"
    # голос -> тот же путь
    groq.replies.append(json.dumps({"actions": [{"type": "pause_business", "enabled": False}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "поставь бизнес-чат на паузу"))
    assert (await db.bconn_get("c1"))["ai_enabled"] == 0


async def test_user_chats_not_leaked_into_prompt_of_other_owner(app, db, groq):
    dp, bot, _ = app
    await owner_with_chats(db)
    user = await db.ensure_user(555, NOW())
    await grants.grant(db, user, 5, ADMIN_ID)
    groq.replies.append(json.dumps({"actions": [{"type": "reply", "text": "ок"}]}))
    await dp.feed_update(bot, message_update(555, "привет"))
    assert "Дилноза" not in groq.prompts[-1][0]["content"] and "Азиз" not in groq.prompts[-1][0]["content"]


async def test_other_agents_cannot_trigger_broadcasts_or_pause(db, tmp_path):
    """Задание от другого агента через шину: рассылки и пауза бизнеса — только от самого владельца."""
    from bot.agents.kimo import is_private_action

    a, b = acts({"type": "create_broadcast", "chat": "1", "time": "10:00", "mode": "text", "text": "x"}, {"type": "pause_business"})
    assert is_private_action(a) and is_private_action(b)
    assert not is_private_action(acts({"type": "open_app"})[0])


async def test_data_isolation_across_all_new_tables(db):
    """Сводный тест: данные одного владельца не видны другому ни в одной таблице этапов 1–5."""
    a, b = await db.ensure_user(901, NOW()), await db.ensure_user(902, NOW())
    await db.add_task(a["id"], "секрет A", NOW(), False, NOW())
    await db.add_expense(a["id"], 100, "UZS", "A", None, None, NOW(), "income")
    await db.set_user_setting(a["id"], "accent", "#ff0000")
    await db.bconn_upsert("conn-a", a["id"], 901, True, True, NOW())
    await db.brules_set(a["id"], NOW(), system_prompt="правила A")
    await db.bchat_touch(a["id"], 5, "Клиент A", None, NOW(), True)
    await db.bmsg_add(a["id"], 5, "client", "сообщение A", 1, NOW())
    sid = await bc.create_schedule(db, a, 5, "10:00", "1234567", "text", "рассылка A", "")
    await db.blog_add(sid, a["id"], 5, "sent", "x", NOW())
    await db.bsub_add(a["id"], 6, "Подписчик A", NOW())
    await db.optout_add(a["id"], 7, NOW())
    await db.advice_set(a["id"], "2026-11-26", "ru", "{}", NOW())
    await grants.grant(db, a, 3, ADMIN_ID)
    far = "2000-01-01 00:00:00", "2100-01-01 00:00:00"
    assert await db.open_tasks(b["id"]) == [] and await db.expenses_between(b["id"], *far, None) == []
    assert await db.user_settings(b["id"]) == {} and await db.user_perm_rows(b["id"]) == {}
    assert await db.bconn_list(b["id"]) == [] and await db.brules_get(b["id"]) is None
    assert await db.bchat_list(b["id"]) == [] and await db.bchat_get(b["id"], 5) is None
    assert await db.bmsg_recent(b["id"], 5) == [] and await db.bmsg_is_ours(b["id"], 5, 1) is False
    assert await db.sched_list(b["id"]) == [] and await db.sched_get(b["id"], sid) is None
    assert await db.blog_list(b["id"]) == [] and await db.blog_sent_since(b["id"], 5, far[0]) == 0
    assert await db.bsub_list(b["id"]) == [] and await db.bsub_get(b["id"], 6) is None
    assert await db.optout_has(b["id"], 7) is False and await db.advice_get(b["id"], "2026-11-26", "ru") is None
    assert await db.grant_active(b["id"]) is None
    # запись «от имени B» чужих строк тоже не проходит
    assert await db.sched_update(b["id"], sid, active=0) is False and await db.bchat_set(b["id"], 5, excluded=1) is False
    assert await db.bmsg_edit(b["id"], 5, 1, "взлом") is False and await db.bmsg_delete(b["id"], 5, [1]) == 0
    assert (await db.bmsg_recent(a["id"], 5))[0]["text"] == "сообщение A"


async def test_agents_and_queue_still_work_after_migrations(db):
    """Мимо, Мими, Миди и очередь заданий: реестр, права и шина работают как раньше."""
    from bot.agents.bus import Bus
    from bot.agents.registry import Registry

    reg = Registry(db)
    await reg.seed_defaults()
    names = {a.name: a.display for a in await reg.all()}
    assert names["kimo"] == "Кимо" and names["agent_builder"] == "Мимо" and names["assistant"] == "Мими"
    kimo = await reg.get("kimo")
    assert {"user_perms", "access_grants", "business_rules", "scheduled_messages"} <= set(kimo.allowed_tables)
    bus_a, bus_k = Bus(db, "assistant", owner_id=ADMIN_ID), Bus(db, "kimo")
    task_id = await bus_a.send("kimo", {"type": "ask", "text": "привет"}, depth=1, chat_id=1)
    claimed = await bus_k.claim()
    assert claimed["id"] == task_id and await bus_k.claim() is None  # атомарный захват
    await bus_k.finish(task_id, "ответ")
    assert (await bus_a.wait_result(task_id, timeout=1))["result"] == "ответ"


async def test_full_downgrade_to_v3_keeps_old_data_and_remigrates(tmp_path):
    """Откат всех новых миграций (v4–v8) сохраняет задачи, расходы и агентов; повторная миграция снова применяется."""
    from bot.db import MIGRATIONS, Database

    db = await Database.connect(tmp_path / "chain.db")
    await db.migrate()
    user = await db.ensure_user(1, NOW())
    await db.add_task(user["id"], "задача", NOW(), False, NOW())
    await db.add_expense(user["id"], 10, "UZS", "Еда", None, None, NOW(), "expense")
    await db.bchat_touch(user["id"], 5, "К", None, NOW(), True)
    await db.downgrade(3)
    assert await db.schema_version() == 3
    tables = {r["name"] for r in await db._all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for gone in ("user_perms", "user_settings", "business_chats", "business_rules", "access_grants", "scheduled_messages", "broadcast_log", "ai_advice"):
        assert gone not in tables, gone
    assert (await db._one("SELECT COUNT(*) AS n FROM tasks"))["n"] == 1 and (await db._one("SELECT COUNT(*) AS n FROM expenses"))["n"] == 1
    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS) == 8
    await db.close()
