"""Отчёты Кимо («кто работает», логи), имена после миграции, Мимо отвечает только по своим агентам."""
import json
from datetime import timedelta

from bot.agents.access import ScopedDB
from bot.agents.bus import Bus, BusWorker
from bot.agents.context import AgentContext
from bot.agents.generic import GenericAgent
from bot.agents.kimo import KimoBusHandler
from bot.agents.registry import Registry
from bot.db import MIGRATIONS, Database
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID, FakeGroq, FakeRates, attach_admin, make_settings
from tests.fakebot import FakeBot, group_message_update, message_update

NOW = "2026-11-10 10:00:00"


async def world(db, tmp_path):
    reg = Registry(db)
    await reg.seed_defaults()
    await db.insert_agent({"name": "midi", "display_name": "Миди", "role": "агент встреч", "enabled": True,
                           "created_by": "agent_builder"}, NOW)
    for name in ("kimo", "assistant", "agent_builder"):
        await db.agent_heartbeat(name, "online", to_db(now_utc()))
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "midi.log").write_text(
        "\n".join(f"2026-11-10 09:00:{i:02d},000 INFO bot: строка {i}" for i in range(15))
        + "\n2026-11-10 09:01:00,000 CRITICAL bot: Агент midi упал: токен 7123456789:AAH" + "x" * 32 + "\n",
        encoding="utf-8",
    )
    return reg, logs


async def test_who_is_working_report(app, db, groq, tmp_path):
    dp, bot, settings = app
    reg, logs = await world(db, tmp_path)
    attach_admin(dp["agent_ctx"], bot, settings, logs)
    # Миди упал: heartbeat 5 минут назад + 1 неудачное задание
    await db._write("UPDATE agents SET last_seen = ?, status = 'online', last_error = ? WHERE name = 'midi'",
                    (to_db(now_utc() - timedelta(minutes=5)), "TelegramNetworkError"))
    t = await Bus(db, "kimo").send("midi", {"type": "ask", "text": "x"})
    await db.queue_force_fail(t, "⚠️ Миди offline: процесс не отвечает", to_db(now_utc()))
    groq.replies.append(json.dumps({"actions": [{"type": "agents_report"}]}))
    await dp.feed_update(bot, group_message_update(ADMIN_ID, "кто работает?"))
    text = bot.texts()[-1]
    assert "работают 3 из 4" in text
    assert "🟢 <b>Кимо</b> — online" in text and "<b>Мими</b>" in text and "<b>Мимо</b>" in text  # по display_name
    assert "⚫ <b>Миди</b> — offline" in text and "Последняя ошибка: TelegramNetworkError" in text
    assert "строка 14" in text and "строка 4<" not in text  # последние 10 записей лога проблемного агента
    assert "AAH" not in text and "***" in text  # токен из лога вырезан
    assert "Последние неудачные задания" in text and "Миди offline" in text
    assert "SELECT" not in text and "|---" not in text  # без SQL и таблиц


async def test_logs_for_last_minutes_and_audit(app, db, groq, tmp_path):
    dp, bot, settings = app
    reg, logs = await world(db, tmp_path)
    attach_admin(dp["agent_ctx"], bot, settings, logs)
    groq.replies.append(json.dumps({"actions": [{"type": "agent_logs", "agent": "Миди", "minutes": 5}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "покажи логи Миди за 5 минут"))
    assert "Лог Миди" in bot.texts()[-1] and "Записей нет" in bot.texts()[-1]  # лог старый (09:00) — за 5 минут пусто
    groq.replies.append(json.dumps({"actions": [{"type": "agent_logs", "agent": "midi", "minutes": None}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "покажи логи Миди"))
    assert "строка 14" in bot.texts()[-1] and "AAH" not in bot.texts()[-1]
    assert [r["action"] for r in await db.audit_list(action="read_log")] == ["read_log", "read_log"]  # чтение логов в аудите


async def test_assistant_asks_kimo_for_agents_and_errors(app, db, tmp_path):
    """Вопрос «о каких агентах и ошибках речь» больше не возвращается: Кимо отвечает отчётом (без логов)."""
    dp, bot, settings = app
    reg, logs = await world(db, tmp_path)
    ctx = dp["agent_ctx"]
    attach_admin(ctx, bot, settings, logs)
    await db._write("UPDATE agents SET last_seen = ?, status = 'online', last_error = 'Groq: GroqUnavailableError' WHERE name = 'midi'",
                    (to_db(now_utc() - timedelta(minutes=5)),))
    llm = FakeGroq()
    llm.replies.append(json.dumps({"actions": [{"type": "agents_report"}, {"type": "manage_agent", "agent": "midi", "op": "stop"}]}))
    scoped = ScopedDB(db, "kimo", ctx.agent.allowed_tables, live=True)
    await scoped.ensure_user(ADMIN_ID, NOW)
    handler = KimoBusHandler(ctx, scoped, llm, FakeRates(scoped), settings)
    assistant_bus = Bus(db, "assistant", owner_id=ADMIN_ID)
    task_id = await assistant_bus.send("kimo", {"type": "ask", "text": "какие агенты с ошибками?"}, chat_id=-100, reply_to=5)
    kimo_bus = Bus(db, "kimo")
    await BusWorker(kimo_bus, handler, FakeBot(), ADMIN_ID).process(await kimo_bus.claim())
    task = await db.queue_get(task_id)
    assert task["status"] == "done" and "Миди" in task["result"] and "offline" in task["result"]
    assert "Лог (последние" not in task["result"]  # через шину логи не отдаются
    assert "Groq: GroqUnavailableError" in task["result"]
    assert await db.pending_open("kimo", to_db(now_utc())) == []  # управление из шины запрещено


async def test_mimo_report_only_own_agents(db, tmp_path):
    reg, logs = await world(db, tmp_path)
    cfg = await reg.get("agent_builder")
    ctx = AgentContext(cfg, reg, db, Bus(db, "agent_builder", owner_id=ADMIN_ID))
    bot = FakeBot()
    settings = make_settings(tmp_path)
    attach_admin(ctx, bot, settings, logs)
    llm = FakeGroq()
    agent = GenericAgent(ctx, llm, settings)
    llm.replies.append(json.dumps({"actions": [{"type": "agents_report"}]}))
    out = await agent.answer_admin("покажи моих агентов")
    assert "Миди" in out and "Мими" not in out and "Кимо" not in out
    llm.replies.append(json.dumps({"actions": [{"type": "agent_logs", "agent": "assistant", "minutes": None}]}))
    out = await agent.answer_admin("покажи логи ассистента")
    assert "Нет прав" in out  # чужой агент: отказ
    llm.replies.append(json.dumps({"actions": [{"type": "manage_agent", "agent": "Миди", "op": "rename", "value": "Миди Плюс"}]}))
    out = await agent.answer_admin("переименуй Миди")
    assert "на подтверждение" in out
    dm = bot.calls[-1]
    assert dm.chat_id == ADMIN_ID and "Изменить имя" in dm.text  # подтверждение — владельцу в личку
    assert (await db.get_agent("midi"))["display_name"] == "Миди"  # до «да» имя прежнее


async def test_names_after_migration(tmp_path):
    """7.7: после миграции Кимо перечисляет агентов по именам, «передай Мими» уходит ассистенту."""
    db = await Database.connect(tmp_path / "old.db")
    for number in (1, 2):
        await db.conn.executescript(f"BEGIN;\n{MIGRATIONS[number - 1]}\nPRAGMA user_version = {number};\nCOMMIT;")
    for name, role in [("kimo", "Главный агент"), ("agent_builder", "бот, который создаёт ботов"),
                       ("assistant", "бот-ассистент"), ("midi", "бот, который назначает встречи")]:
        await db._write("INSERT INTO agents (name, role, enabled, config, created_at) VALUES (?, ?, 1, '{}', ?)", (name, role, NOW))
    await db.migrate()
    reg = Registry(db)
    names = {a.name: a.display_name for a in await reg.all()}
    assert names["kimo"] == "Кимо" and names["agent_builder"] == "Мимо" and names["assistant"] == "Мими"
    assert names["midi"] == "midi"  # описание НЕ стало именем; имя задаётся владельцем
    assert (await reg.find("мими")).name == "assistant" and (await reg.find("ASSISTANT")).name == "assistant"
    assert (await reg.find("Мимо")).name == "agent_builder"
    await db.close()


async def test_kimo_transfers_to_mimi_by_display_name(app, db, groq):
    dp, bot, _ = app
    groq.replies.append(json.dumps({"actions": [{"type": "delegate", "agent": "МИМИ", "task": "найди рецепт плова"}]}))
    await dp.feed_update(bot, group_message_update(ADMIN_ID, "передай Мими найти рецепт"))
    task = await db.queue_claim("assistant", NOW)  # агент в очереди — технический id assistant
    assert task["to_agent"] == "assistant" and task["payload"]["text"] == "найди рецепт плова"
    assert "Мими" in bot.texts()[-1]
