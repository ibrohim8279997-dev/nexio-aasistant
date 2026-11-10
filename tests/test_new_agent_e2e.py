"""5.3: Мимо создаёт агента -> он зарегистрирован до запуска -> поднимается -> берёт задание -> пишет result -> Кимо получает ответ."""
import asyncio
import json

from bot.agents import builder_flow as flow
from bot.agents import procs
from bot.agents.cards import parse_card
from tests.conftest import ADMIN_ID, attach_admin
from tests.simproc import SimControl

TOKEN = "7123456789:AAH" + "x" * 32


async def test_created_agent_comes_up_takes_task_and_kimo_gets_answer(app, db, groq, tmp_path, monkeypatch):
    dp, bot, settings = app
    ctx = dp["agent_ctx"]
    logs = tmp_path / "logs"
    attach_admin(ctx, bot, settings, logs)
    dp["confirm"] = ctx.admin.confirm
    sim = SimControl(db, logs, ADMIN_ID)
    procs.set_control(sim)

    async def fake_check(token):
        return "nexio_staff_bot"

    monkeypatch.setattr(flow, "check_token", fake_check)
    try:
        # 1) Мимо составил карточку; агент зарегистрирован в agents ДО запуска процесса
        card = parse_card(json.dumps({
            "name": "staffbot", "display_name": "Бот сотрудников", "role": "Отвечает сотрудникам про Nexio CRM",
            "system_prompt": "Помогаешь сотрудникам с Nexio CRM.", "needs_stt": False, "suggested_tables": ["tasks"],
        }))
        await flow.save_draft(ctx, card)
        row = await db.get_agent("staffbot")
        assert row["enabled"] is False and row["created_by"] == "agent_builder" and row["display_name"] is None
        assert row["models"]["provider"] == "groq" and row["models"]["text"] == "openai/gpt-oss-120b"  # модели записаны в конфиг
        assert row["config"]["token_env"] == "STAFFBOT_TOKEN" and row["name"] == "staffbot"  # имя в очереди = agent_id
        assert row["allowed_tables"] == []  # по умолчанию без доступа к таблицам
        assert "gsk_" not in json.dumps(row)  # ключ Groq в конфиг не пишется: берётся из .env процесса

        # 2) права -> имя -> «да» -> токен; до этого агент не запускается
        await flow.approve(ctx, "staffbot", with_suggested=False)
        assert sim.calls == []
        proposal = await flow.propose_name(ctx, ctx.admin.confirm, "staffbot", "Штаб")
        await ctx.admin.confirm.decide(proposal.id, True, ADMIN_ID, True)
        assert (await db.get_agent("staffbot"))["display_name"] == "Штаб"
        from bot.crypto import Crypto, SecretStore
        secrets = SecretStore(db, Crypto(settings.encryption_key))
        username = await flow.accept_token(ctx, secrets, "staffbot", TOKEN)
        assert username == "nexio_staff_bot" and (await db.get_agent("staffbot"))["enabled"] is True
        stored = await db.get_secret("bot_token:staffbot")
        assert stored and TOKEN not in stored

        # 3) процесс поднимается (отдельное окно, только он), heartbeat идёт
        ok, _ = await flow.launch("staffbot")
        assert ok and sim.calls == [("start", "staffbot")]
        assert await procs.wait_online(db, "staffbot", "2000-01-01 00:00:00", timeout=3)

        # 4) Кимо ставит задание, агент берёт его под своим именем, пишет result и status, Кимо получает ответ
        kimo_bus = ctx.bus
        task_id = await kimo_bus.send("staffbot", {"type": "ask", "text": "как создать клиента?"}, chat_id=-100, reply_to=9)
        task = await _wait_done(db, task_id)
        assert task["status"] == "done" and task["result"] == "ok" and task["to_agent"] == "staffbot"
        assert await kimo_bus.request("staffbot", {"type": "ping"}, timeout=3) == "pong"
    finally:
        await sim.shutdown()
        procs.set_control(procs.TmuxControl())


async def _wait_done(db, task_id):
    for _ in range(60):
        task = await db.queue_get(task_id)
        if task["status"] in ("done", "failed"):
            return task
        await asyncio.sleep(0.1)
    raise AssertionError("агент не взял задание")
