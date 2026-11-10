"""Самолечение: полный отчёт -> «да» в личке -> бэкап -> правка -> перезапуск одного агента -> ping; откат; ограничения."""
import json
from html import unescape

import pytest

from bot.agents import procs
from bot.agents.confirm import ConfirmError
from bot.agents.selfheal import MAX_ATTEMPTS_PER_HOUR, SelfHeal
from bot.keyboards import ConfirmCb
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID, attach_admin
from tests.fakebot import callback_update, message_update
from tests.simproc import SimControl

NOW = "2026-11-10 10:00:00"
GOOD_MODELS = {"provider": "groq", "text": "openai/gpt-oss-120b", "stt": None, "search": False}
BROKEN_MODELS = {"provider": "groq", "text": "", "stt": None, "search": False}


@pytest.fixture
async def lab(app, db, tmp_path):
    dp, bot, settings = app
    logs = tmp_path / "logs"
    logs.mkdir()
    ctx = dp["agent_ctx"]
    attach_admin(ctx, bot, settings, logs)
    ctx.admin.selfheal = SelfHeal(ctx, ctx.admin.ops, ctx.admin.confirm, online_timeout=1.5, ping_timeout=1.5)
    dp["confirm"] = ctx.admin.confirm  # кнопки «Да/Нет» обрабатывает тот же Confirmations, где зарегистрирован исполнитель
    sim = SimControl(db, logs, ADMIN_ID)
    procs.set_control(sim)
    await db.insert_agent({"name": "dummy", "display_name": "Дамми", "role": "тестовый агент", "enabled": True,
                           "created_by": "agent_builder", "models": BROKEN_MODELS}, NOW)
    yield dp, bot, ctx, sim, db
    await sim.shutdown()
    procs.set_control(procs.TmuxControl())


def dm_with_buttons(bot):
    return next(c for c in reversed(bot.calls) if getattr(c, "reply_markup", None) and "Подтвердите" in (c.text or ""))


def pending_id(bot):
    return ConfirmCb.unpack(dm_with_buttons(bot).reply_markup.inline_keyboard[0][0].callback_data).id


async def ask_diagnose(dp, bot, groq, agent="Дамми"):
    groq.replies.append(json.dumps({"actions": [{"type": "diagnose_agent", "agent": agent}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, f"почему {agent} не работает?"))


async def test_full_cycle_report_yes_fix_ping(lab, groq):
    dp, bot, ctx, sim, db = lab
    await ask_diagnose(dp, bot, groq)
    report = unescape(dm_with_buttons(bot).text)
    for part in ("Что сломалось", "models.text", "таблица agents", "Как определил", "Что хочу изменить",
                 "Зачем", "Что может затронуть", "Как проверю", "ping"):
        assert part in report, part
    assert '- models.text: ""' in report and '+ models.text: "openai/gpt-oss-120b"' in report  # diff
    # без «да» ничего не изменено и ничего не запущено
    assert (await db.get_agent("dummy"))["models"]["text"] == "" and sim.calls == []
    pid = pending_id(bot)

    await dp.feed_update(bot, callback_update(ADMIN_ID, ConfirmCb(id=pid, yes=1).pack()))
    assert sim.calls == [("stop", "dummy"), ("start", "dummy")]  # только процесс этого агента
    assert "применено, агент online, ping прошёл" in bot.texts()[-1]
    row = await db.get_agent("dummy")
    assert row["models"]["text"] == "openai/gpt-oss-120b" and row["status"] in ("online", "busy")
    backups = list((tmp_path_backups(db)).glob("agent-dummy-*.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text())["models"]["text"] == ""  # бэкап прежней конфигурации
    audit = {r["action"]: r for r in await db.audit_list(target="dummy", limit=50)}
    assert audit["selfheal_apply"]["report_id"] == pid and audit["selfheal_apply"]["confirmed_by"] == ADMIN_ID
    assert audit["selfheal_ok"]["result"] == "ok"
    assert (await db.get_agent("assistant"))["status"] == "online"  # остальных агентов не трогали


def tmp_path_backups(db):
    return db.backup_dir()


async def test_rollback_when_fix_does_not_help(lab, groq):
    dp, bot, ctx, sim, db = lab
    sim.always_fail.add("dummy")  # падает по причине, которую конфигурацией не вылечить
    await ask_diagnose(dp, bot, groq)
    pid = pending_id(bot)
    await dp.feed_update(bot, callback_update(ADMIN_ID, ConfirmCb(id=pid, yes=1).pack()))
    text = bot.texts()[-1]
    assert "не сработало" in text and "heartbeat не пришёл" in text and "возвращена из бэкапа" in text
    assert (await db.get_agent("dummy"))["models"]["text"] == ""  # откат: прежняя конфигурация
    assert sim.calls == [("stop", "dummy"), ("start", "dummy"), ("stop", "dummy"), ("start", "dummy")]  # старая версия запущена снова
    actions = [r["action"] for r in await db.audit_list(target="dummy", limit=50)]
    assert "selfheal_rollback" in actions and "selfheal_ok" not in actions


async def test_no_change_without_yes_or_after_no(lab, groq):
    dp, bot, ctx, sim, db = lab
    await ask_diagnose(dp, bot, groq)
    pid = pending_id(bot)
    await dp.feed_update(bot, callback_update(ADMIN_ID, ConfirmCb(id=pid, yes=0).pack()))
    assert (await db.get_agent("dummy"))["models"]["text"] == "" and sim.calls == []
    assert await db.audit_list(target="dummy", action="selfheal_apply") == []


async def test_kimo_and_mimo_are_never_auto_fixed(lab, groq):
    dp, bot, ctx, sim, db = lab
    await db._write("UPDATE agents SET models = ? WHERE name IN ('kimo', 'agent_builder')", (json.dumps(BROKEN_MODELS),))
    for query, name in (("Кимо", "kimo"), ("Мимо", "agent_builder")):
        await ask_diagnose(dp, bot, groq, query)
        report = unescape(bot.texts()[-1])
        assert "Правка не применяется" in report and "Кимо или Мимо" in report and '+ models.text' in report  # diff для владельца
    assert await db.pending_open("kimo", to_db(now_utc())) == []
    assert (await db.get_agent("kimo"))["models"]["text"] == "" and sim.calls == []
    # запрос на правку кода/прав самого Кимо отклоняется и на уровне действий
    with pytest.raises(ConfirmError):
        await ctx.admin.confirm.propose("set_prompt", "kimo", {"value": "новый промпт Кимо"})
    prop = await ctx.admin.confirm.propose_raw("apply_fix", "kimo", {"changes": {"models": GOOD_MODELS}, "diff": []}, "x")
    result = await ctx.admin.confirm.decide(prop.id, True, ADMIN_ID, True)
    assert "автоматически не правятся" in result and (await db.get_agent("kimo"))["models"]["text"] == ""


async def test_at_most_two_attempts_per_hour(lab, groq):
    dp, bot, ctx, sim, db = lab
    sim.always_fail.add("dummy")
    for _ in range(MAX_ATTEMPTS_PER_HOUR):
        await ask_diagnose(dp, bot, groq)
        await dp.feed_update(bot, callback_update(ADMIN_ID, ConfirmCb(id=pending_id(bot), yes=1).pack()))
    dms_before = sum(1 for c in bot.calls if getattr(c, "reply_markup", None) and "Подтвердите" in (c.text or ""))
    await ask_diagnose(dp, bot, groq)
    assert "лимит 2 попыток" in bot.texts()[-1] and "только отчёт" in bot.texts()[-1]
    dms_after = sum(1 for c in bot.calls if getattr(c, "reply_markup", None) and "Подтвердите" in (c.text or ""))
    assert await db.pending_open("kimo", to_db(now_utc())) == [] and dms_after == dms_before  # новое подтверждение не создано


async def test_cause_not_found_says_so_and_changes_nothing(lab, groq):
    dp, bot, ctx, sim, db = lab
    await db.update_agent("dummy", models=GOOD_MODELS)  # конфигурация в порядке, логов нет
    await ask_diagnose(dp, bot, groq)
    text = bot.texts()[-1]
    assert "Причину найти не удалось" in text and "Ничего не выдумываю" in text
    assert await db.pending_open("kimo", to_db(now_utc())) == []


async def test_log_causes_reported_without_config_fix(lab, groq):
    dp, bot, ctx, sim, db = lab
    await db.update_agent("dummy", models=GOOD_MODELS)
    (ctx.admin.ops.log_dir / "dummy.log").write_text(
        "2026-11-10 09:00:00,000 ERROR bot: Ошибка конфигурации: Нет токена для dummy: задайте DUMMY_TOKEN\n"
        'Traceback (most recent call last):\n  File "/home/user/nexio-aasistant/bot/runner.py", line 77, in run\n',
        encoding="utf-8")
    await ask_diagnose(dp, bot, groq)
    text = bot.texts()[-1]
    assert "не задан токен" in text and "set-token" in text and "bot/runner.py:77" in text
    assert await db.pending_open("kimo", to_db(now_utc())) == []  # токен — дело владельца, правка конфигурации не нужна


async def test_mimo_diagnoses_only_own_agents(lab, groq, tmp_path):
    from bot.agents.admin_ops import AdminError
    from bot.agents.context import AgentContext
    from bot.agents.bus import Bus
    dp, bot, ctx, sim, db = lab
    reg = ctx.registry
    mimo_cfg = await reg.get("agent_builder")
    mimo_ctx = AgentContext(mimo_cfg, reg, db, Bus(db, "agent_builder", owner_id=ADMIN_ID))
    attach_admin(mimo_ctx, bot, dp["settings"], ctx.admin.ops.log_dir)
    mimo_ctx.admin.selfheal = SelfHeal(mimo_ctx, mimo_ctx.admin.ops, mimo_ctx.admin.confirm, online_timeout=3.0, ping_timeout=3.0)
    _, text = await mimo_ctx.admin.selfheal.diagnose("Дамми")  # создан Мимо
    assert "models.text" in text
    with pytest.raises(AdminError):
        await mimo_ctx.admin.selfheal.diagnose("assistant")  # чужой


async def test_midi_stop_call_offline_restart_online_report(lab, groq):
    """8.8: остановить Миди, вызвать через Кимо -> сразу «Миди offline» с причиной; перезапуск после «да» -> online; «кто работает»."""
    dp, bot, ctx, sim, db = lab
    await db.update_agent("dummy", display_name="Миди", models=GOOD_MODELS)  # «Миди» = агент встреч
    await sim.start("dummy")
    for _ in range(40):  # ждём первый heartbeat
        if (await db.get_agent("dummy"))["status"] == "online":
            break
        import asyncio
        await asyncio.sleep(0.1)
    sim.calls.clear()
    await sim.stop("dummy")  # процесс остановлен
    groq.replies.append(json.dumps({"actions": [{"type": "delegate", "agent": "Миди", "task": "назначь встречу"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "передай Миди назначить встречу на пятницу"))
    answer = unescape(bot.texts()[-1])
    assert "Миди offline" in answer and "процесс не отвечает" in answer and "Задание не передано" in answer
    assert await db.queue_claim("dummy", NOW) is None  # задание не создано
    confirm_text = unescape(dm_with_buttons(bot).text)
    assert "Перезапустить агента Миди" in confirm_text  # предложение перезапуска, ждёт «да»
    assert ("start", "dummy") not in sim.calls  # перезапуск только после «да»

    await dp.feed_update(bot, callback_update(ADMIN_ID, ConfirmCb(id=pending_id(bot), yes=1).pack()))
    assert "перезапущен и онлайн" in bot.texts()[-1]
    assert (await db.get_agent("dummy"))["status"] in ("online", "busy")

    groq.replies.append(json.dumps({"actions": [{"type": "agents_report"}]}))
    await dp.feed_update(bot, message_update(ADMIN_ID, "кто работает?"))
    report = unescape(bot.texts()[-1])
    assert "Миди — online" in report.replace("<b>", "").replace("</b>", "")
