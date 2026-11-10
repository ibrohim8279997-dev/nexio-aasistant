"""Очередь: контекст, чат владельца, доставка результата отправителем (один раз), failed вместо вопроса, depth."""
import asyncio
import json

import pytest

from bot.agents.access import ScopedDB
from bot.agents.bus import MAX_DEPTH, Bus, BusError, BusWorker, DepthExceeded
from bot.agents.context import AgentContext
from bot.agents.generic import GenericAgent
from bot.agents.kimo import KimoBusHandler
from bot.agents.registry import Registry
from tests.conftest import ADMIN_ID, FakeGroq, FakeRates, make_settings
from tests.fakebot import FakeBot

CHAT, MSG = -100500, 77


def reply_id(call):
    params = getattr(call, "reply_parameters", None)
    return params.message_id if params else call.reply_to_message_id


async def kimo_stack(db, settings, llm):
    reg = Registry(db)
    await reg.seed_defaults()
    cfg = await reg.get("kimo")
    scoped = ScopedDB(db, "kimo", cfg.allowed_tables, role="admin")
    owner = await scoped.ensure_user(ADMIN_ID, "2026-11-10 00:00:00")
    ctx = AgentContext(cfg, reg, db, Bus(db, "kimo", owner_id=ADMIN_ID))
    return KimoBusHandler(ctx, scoped, llm, FakeRates(scoped), settings), owner


async def test_assistant_ask_kimo_with_context_reply_in_chat(db, tmp_path):
    """5.1: ассистент создаёт ask с контекстом, Кимо отвечает, ответ приходит в чат реплаем (один раз)."""
    settings = make_settings(tmp_path)
    kimo_llm = FakeGroq()
    kimo_llm.replies.append('{"actions":[{"type":"list_tasks","day":"today"}]}')
    handler, owner = await kimo_stack(db, settings, kimo_llm)
    await db.add_task(owner["id"], "позвонить в банк", "2026-11-10 06:00:00", True, "2026-11-10 00:00:00")

    asst_bot, kimo_bot = FakeBot(), FakeBot()
    assistant_bus = Bus(db, "assistant", owner_id=ADMIN_ID, display="Мими")
    # задание создано «из сообщения» в группе и НЕ ждётся внутри процесса: результат заберёт цикл доставки
    task_id = await assistant_bus.send(
        "kimo", {"type": "ask", "text": "какие у меня напоминания?", "context": {"owner_request": "что у меня на сегодня?"}},
        chat_id=CHAT, reply_to=MSG,
    )
    task = await db.queue_get(task_id)
    assert task["chat_id"] == CHAT and task["reply_to_message_id"] == MSG and task["reply"] == 1
    assert task["payload"]["reply"] is True and task["payload"]["context"]["from"] == "assistant"

    kimo_worker = BusWorker(Bus(db, "kimo"), handler, kimo_bot, ADMIN_ID)
    await kimo_worker.process(await Bus(db, "kimo").claim())
    done = await db.queue_get(task_id)
    assert done["status"] == "done" and "позвонить в банк" in done["result"]
    assert kimo_bot.calls == []  # Кимо сам в чат не пишет: отвечает отправитель
    assert "что у меня на сегодня?" in kimo_llm.prompts[0][0]["content"]  # контекст дошёл до модели

    asst_worker = BusWorker(assistant_bus, None, asst_bot, ADMIN_ID)
    assert await asst_worker.deliver_results() == 1
    sent = asst_bot.calls[-1]
    assert sent.chat_id == CHAT and reply_id(sent) == MSG and "позвонить в банк" in sent.text
    # 5.6: повторной доставки нет
    assert await asst_worker.deliver_results() == 0 and len(asst_bot.calls) == 1
    assert (await db.queue_get(task_id))["delivered"] == 1
    other_loop = BusWorker(assistant_bus, None, FakeBot(), ADMIN_ID)  # второй процесс/цикл того же агента
    assert await other_loop.deliver_results() == 0


async def test_task_without_chat_goes_to_owner_chat(db):
    """5.2: задание без chat_id получает чат владельца и пометку в payload."""
    bus = Bus(db, "assistant", owner_id=ADMIN_ID)
    task_id = await bus.send("kimo", {"type": "ask", "text": "q"})
    task = await db.queue_get(task_id)
    assert task["chat_id"] == ADMIN_ID and task["payload"]["chat_fallback"] is True
    assert task["payload"]["context"]["auto"] is True and task["payload"]["context"]["about"] == "q"  # контекст дополнен

    explicit = await db.queue_get(await bus.send("kimo", {"type": "ask", "text": "q"}, chat_id=CHAT, reply_to=5))
    assert explicit["chat_id"] == CHAT and "chat_fallback" not in explicit["payload"]

    ping = await db.queue_get(await bus.send("kimo", {"type": "ping"}))
    assert ping["reply"] == 0 and ping["payload"]["reply"] is False  # reply=true только по умолчанию у ask


async def test_inline_waiter_marks_delivered_no_duplicate(db):
    kimo, assistant = Bus(db, "kimo"), Bus(db, "assistant", owner_id=ADMIN_ID)

    async def handler(task):
        return "42"

    worker = BusWorker(kimo, handler, FakeBot(), ADMIN_ID, interval=0.05)
    runner = asyncio.create_task(worker.run())
    try:
        assert await assistant.ask("kimo", "сколько?", chat_id=CHAT, reply_to=MSG, timeout=5) == "42"
    finally:
        worker.stop()
        await runner
    bot = FakeBot()
    assert await BusWorker(assistant, None, bot, ADMIN_ID).deliver_results() == 0 and bot.calls == []


async def test_late_result_is_not_lost(db, monkeypatch):
    """Ждали, не дождались (таймаут), результат пришёл позже — доставляется отправителем, а не теряется."""
    import bot.agents.bus as bus_mod

    assistant, kimo = Bus(db, "assistant", owner_id=ADMIN_ID), Bus(db, "kimo")
    with pytest.raises(BusError):
        await assistant.ask("kimo", "медленный вопрос", timeout=0.1, chat_id=CHAT, reply_to=MSG)
    task = await kimo.claim()
    await kimo.finish(task["id"], "ответ позже", True)
    bot = FakeBot()
    worker = BusWorker(assistant, None, bot, ADMIN_ID)
    assert await worker.deliver_results() == 0  # ещё в окне ожидания
    monkeypatch.setattr(bus_mod, "LATE_GRACE", -1000.0)
    assert await worker.deliver_results() == 1
    assert "ответ позже" in bot.calls[-1].text and "с опозданием" in bot.calls[-1].text


async def test_failed_result_delivered_once_with_reason(db):
    assistant, kimo = Bus(db, "assistant", owner_id=ADMIN_ID), Bus(db, "kimo")
    task_id = await assistant.send("kimo", {"type": "ask", "text": "x"}, chat_id=CHAT, reply_to=MSG)
    await kimo.claim()
    await kimo.finish(task_id, "Кимо не смог выполнить задание: причина", ok=False)
    bot = FakeBot()
    worker = BusWorker(assistant, None, bot, ADMIN_ID)
    assert await worker.deliver_results() == 1 and bot.calls[-1].text.startswith("⚠️")
    assert await worker.deliver_results() == 0


async def test_kimo_clarification_is_failed_and_question_to_owner(db, tmp_path):
    """Кимо не понял — failed с причиной, а вопрос уходит владельцу, а не молча done."""
    llm = FakeGroq()
    llm.replies.append('{"actions":[{"type":"clarify","question":"О каких агентах и ошибках речь?"}]}')
    handler, _ = await kimo_stack(db, make_settings(tmp_path), llm)
    sender = Bus(db, "assistant", owner_id=ADMIN_ID)
    task_id = await sender.send("kimo", {"type": "ask", "text": "что с ошибками?"}, chat_id=CHAT, reply_to=MSG)
    bot = FakeBot()
    await BusWorker(Bus(db, "kimo"), handler, bot, ADMIN_ID).process(await Bus(db, "kimo").claim())
    task = await db.queue_get(task_id)
    assert task["status"] == "failed" and "нужно уточнение" in task["result"]
    assert bot.calls[-1].chat_id == ADMIN_ID and "О каких агентах" in bot.calls[-1].text


async def test_depth_stops_after_three_handoffs(db):
    """5.5: защита от зацикливания."""
    a, b = Bus(db, "assistant", owner_id=ADMIN_ID), Bus(db, "kimo", owner_id=ADMIN_ID)
    task_id = await a.send("kimo", {"type": "ask", "text": "x"}, depth=1, chat_id=CHAT)
    task = await db.queue_get(task_id)
    for i in range(2, MAX_DEPTH + 1):
        sender, target = (b, "assistant") if i % 2 == 0 else (a, "kimo")
        task = await db.queue_get(await sender.forward(task, target))
        assert task["depth"] == i
    with pytest.raises(DepthExceeded) as info:
        await a.forward(task, "kimo")
    stopped = await db.queue_get(info.value.task_id)
    assert stopped["status"] == "failed" and stopped["delivered"] == 1 and "больше 3" in stopped["result"]


async def test_receiver_mode_reply_once(db):
    kimo, mimi = Bus(db, "kimo", owner_id=ADMIN_ID), Bus(db, "assistant")
    task_id = await kimo.send("assistant", {"type": "ask", "text": "q", "deliver_by": "receiver"}, chat_id=CHAT, reply_to=MSG)

    async def handler(t):
        return "привет"

    bot = FakeBot()
    await BusWorker(mimi, handler, bot, ADMIN_ID).process(await mimi.claim())
    assert len(bot.calls) == 1 and reply_id(bot.calls[0]) == MSG
    kimo_bot = FakeBot()
    assert await BusWorker(kimo, None, kimo_bot, ADMIN_ID).deliver_results() == 0  # Кимо не дублирует
    assert (await db.queue_get(task_id))["delivered"] == 1
