"""Этап 4: агент звонит владельцу. Только Кимо и только по call_owner; 1 звонок за 10 минут; ночью только срочные;
30 секунд без ответа -> отбой и сообщение в Telegram; «Linphone спит» -> причина в лог и в Telegram."""
import asyncio
import json
import logging
from datetime import datetime, timedelta

import pytest

from bot.agents.bus import Bus, BusError
from bot.agents.generic import GenericAgent
from bot.timeutil import TZ, now_utc, to_db
from bot.voice.outbound import NoBot, OutboundCaller, ServiceWorker, describe_failure
from bot.voice.policy import MIN_INTERVAL, check_call, is_night
from bot.voice.service import VoiceService
from bot.voice.session import VoiceDeps
from tests.conftest import ADMIN_ID, FakeGroq
from tests.test_voice_stage2 import FakeAsr, FakeTts, cfg_for
from tests.test_voice_stage3 import Rig
from tests.voice_fakes import FakeSipCall


def at(hour, minute=0):
    return datetime(2026, 11, 16, hour, minute, tzinfo=TZ)


# ---------- правила ----------

def test_night_window_is_23_to_07_tashkent():
    assert [is_night(at(h)) for h in (22, 23, 0, 6, 7)] == [False, True, True, True, False]
    assert is_night(at(22, 59)) is False and is_night(at(6, 59)) is True


def test_policy_rate_limit_night_and_urgent():
    assert check_call(at(12), None, False).ok
    assert not check_call(at(12, 5), at(12, 0), False).ok and "10 минут" in check_call(at(12, 5), at(12, 0), False).reason
    assert check_call(at(12, 10), at(12, 0), False).ok  # ровно 10 минут прошло
    night = check_call(at(23, 30), None, False)
    assert not night.ok and "ночное" in night.reason
    assert check_call(at(3), None, True).ok  # срочное ночью разрешено
    assert not check_call(at(3, 5), at(3, 0), True).ok  # но не чаще одного звонка за 10 минут
    assert MIN_INTERVAL == timedelta(minutes=10)


def test_failure_descriptions_name_android_sleep():
    assert "Android усыпил" in describe_failure(False, "", True, 30)
    assert describe_failure(True, "", True, 30) == "владелец не ответил за 30 с"
    assert "отклонил" in describe_failure(True, "603 Decline", False, 30)
    assert "Android" in describe_failure(False, "408 Request Timeout", False, 30)


# ---------- служба звонков ----------

class OutStack:
    """SIP-стек с исходящими звонками: тест сам изображает поведение Linphone."""

    def __init__(self):
        self.calls, self.handlers, self.uris = [], [], []
        self.script = None  # корутина-сценарий: что делает «Linphone» после начала звонка

    def call_out(self, uri, handler):
        call = FakeSipCall(str(len(self.calls) + 1), uri, "out")
        self.calls.append(call)
        self.handlers.append(handler)
        self.uris.append(uri)
        if self.script:
            asyncio.get_running_loop().create_task(self.script(call, handler))
        return call


async def ringing_then_answer(call, handler):
    await asyncio.sleep(0.05)
    handler.on_early()
    await asyncio.sleep(0.05)
    handler.on_connected()


async def ringing_no_answer(call, handler):
    await asyncio.sleep(0.05)
    handler.on_early()


async def phone_asleep(call, handler):
    return None  # Android усыпил Linphone: даже 180 Ringing не приходит


async def declined(call, handler):
    await asyncio.sleep(0.05)
    handler.on_early()
    handler.on_ended("603 Decline")


async def build(db, tmp_path, script=ringing_then_answer, ring=1, callback="sip:owner@100.64.0.7:5060"):
    rig = await Rig().build(db, tmp_path)
    await db.update_agent("voice_sip", enabled=True)
    await db.agent_heartbeat("voice_sip", "online", to_db(now_utc()))
    cfg = cfg_for(outbound_ring_seconds=ring, callback_uri=callback)
    rig.cfg = cfg
    stack = OutStack()
    stack.script = script
    service = VoiceService(cfg, db, rig.state, lambda c, s: None)
    synth = FakeTts()
    deps = VoiceDeps(FakeAsr(), synth, rig.link, rig.relay, cfg)
    caller = OutboundCaller(stack, service, deps)
    rig.voice_worker = ServiceWorker(rig.voice_bus, caller.place, NoBot(), ADMIN_ID, interval=0.05)
    rig.voice_runner = asyncio.create_task(rig.voice_worker.run())
    rig.stack, rig.service, rig.synth, rig.caller = stack, service, synth, caller
    rig.handler.call_wait = 5.0
    return rig


async def close(rig):
    rig.voice_worker.stop()
    await rig.voice_runner
    await rig.close()


async def request_call(bus, reason="сервер упал", urgent=False, timeout=10):
    return await bus.request("kimo", {"type": "call_owner", "reason": reason, "urgent": urgent}, timeout=timeout)


async def tg_texts(rig, wait=1.5):
    await asyncio.sleep(wait)
    return [getattr(c, "text", "") or "" for c in rig.bot.calls]


async def test_call_reaches_linphone_and_speaks_reason(db, tmp_path):
    rig = await build(db, tmp_path)
    try:
        asst = Bus(db, "assistant", owner_id=ADMIN_ID)
        assert "поставлен в очередь" in await request_call(asst, "Миди не отвечает")
        for _ in range(60):
            if rig.stack.calls and rig.service.busy():
                break
            await asyncio.sleep(0.1)
        assert rig.stack.uris == ["sip:owner@100.64.0.7:5060"]  # адрес Linphone из SIP_CALLBACK_URI
        await asyncio.sleep(0.8)
        assert ("Кимо на связи. Миди не отвечает", "ru") in rig.synth.spoken  # владельцу сказали причину
        audit = await db.audit_list(action="voice_call_out")
        assert audit and audit[0]["actor"] == "kimo" and json.loads(audit[0]["details"])["from"] == "assistant"
        task = await db._one("SELECT * FROM tasks_queue WHERE to_agent = 'voice_sip' ORDER BY id DESC LIMIT 1")
        assert task["from_agent"] == "kimo" and task["status"] == "done" and "ответил" in task["result"] and task["depth"] == 2
        assert not any("Не дозвонился" in t for t in await tg_texts(rig, 0.3))
        rig.stack.handlers[0].on_ended("200 OK")
    finally:
        await close(rig)


async def test_no_answer_hangs_up_and_writes_to_telegram(db, tmp_path):
    rig = await build(db, tmp_path, script=ringing_no_answer, ring=1)
    try:
        await request_call(Bus(db, "assistant", owner_id=ADMIN_ID), "нужен ответ по счёту")
        texts = await tg_texts(rig, 2.5)
        assert rig.stack.calls[0].hung_up  # трубка повешена
        assert any("Не дозвонился" in t and "нужен ответ по счёту" in t and "не ответил" in t for t in texts)
        assert not rig.service.busy()  # линия освобождена
    finally:
        await close(rig)


async def test_android_asleep_logged_and_duplicated_to_telegram(db, tmp_path, caplog):
    rig = await build(db, tmp_path, script=phone_asleep, ring=1)
    try:
        with caplog.at_level(logging.WARNING, logger="voice.out"):
            await request_call(Bus(db, "assistant", owner_id=ADMIN_ID), "срочный вопрос")
            texts = await tg_texts(rig, 2.5)
        assert "Android усыпил" in caplog.text  # причина в логе
        assert any("Не дозвонился" in t and "Android усыпил" in t for t in texts)  # и сообщением в Telegram
        assert not rig.service.busy()
    finally:
        await close(rig)


async def test_declined_call_reports_reason(db, tmp_path):
    rig = await build(db, tmp_path, script=declined, ring=2)
    try:
        await request_call(Bus(db, "assistant", owner_id=ADMIN_ID), "вопрос")
        assert any("отклонил" in t for t in await tg_texts(rig, 1.5))
    finally:
        await close(rig)


async def test_only_one_call_per_10_minutes(db, tmp_path):
    rig = await build(db, tmp_path)
    try:
        bus = Bus(db, "assistant", owner_id=ADMIN_ID)
        await request_call(bus, "первый")
        with pytest.raises(BusError) as info:
            await request_call(bus, "второй")
        assert "10 минут" in str(info.value)
        assert any("Хотел позвонить" in t and "второй" in t for t in await tg_texts(rig, 0.5))  # суть не потеряна
        await asyncio.sleep(0.5)
        assert len(rig.stack.calls) == 1  # второго звонка не было
        assert len(await db.audit_list(action="voice_call_out")) == 1
    finally:
        await close(rig)


async def test_night_only_urgent_calls(db, tmp_path):
    rig = await build(db, tmp_path)
    try:
        rig.handler.clock = lambda: at(23, 30)
        bus = Bus(db, "assistant", owner_id=ADMIN_ID)
        with pytest.raises(BusError) as info:
            await request_call(bus, "не срочно")
        assert "ночное" in str(info.value) and rig.stack.calls == []
        assert any("не срочно" in t and "Не звоню" in t for t in await tg_texts(rig, 0.5))
        assert "поставлен" in await request_call(bus, "горит", urgent=True)  # срочное ночью проходит
        await asyncio.sleep(0.5)
        assert len(rig.stack.calls) == 1
        rig.stack.handlers[0].on_ended("bye")
    finally:
        await close(rig)


async def test_only_kimo_can_order_a_call(db, tmp_path):
    rig = await build(db, tmp_path)
    try:
        for sender in ("assistant", "agent_builder", "midi"):
            bus = Bus(db, sender, owner_id=ADMIN_ID)
            with pytest.raises(BusError) as info:
                await bus.request("voice_sip", {"type": "place_call", "reason": "обход Кимо"}, timeout=5)
            assert "только по заданию Кимо" in str(info.value)
        assert rig.stack.calls == []
    finally:
        await close(rig)


async def test_voice_service_disabled_or_offline_or_no_reason(db, tmp_path):
    rig = await build(db, tmp_path)
    try:
        bus = Bus(db, "assistant", owner_id=ADMIN_ID)
        with pytest.raises(BusError):
            await request_call(bus, "  ")
        await db._write("UPDATE agents SET last_seen = '2020-01-01 00:00:00' WHERE name = 'voice_sip'")
        with pytest.raises(BusError) as info:
            await request_call(bus, "служба упала")
        assert "offline" in str(info.value)
        assert any("Голосовая служба offline" in t for t in await tg_texts(rig, 0.4))
        await db.update_agent("voice_sip", enabled=False)
        with pytest.raises(BusError):
            await request_call(bus, "служба выключена")
        assert await db.audit_list(action="voice_call_out") == []  # попытки не засчитаны
    finally:
        await close(rig)


async def test_agent_planner_can_ask_kimo_to_call(db, tmp_path):
    rig = await build(db, tmp_path)
    try:
        cfg = await rig.ctx.registry.get("assistant")
        from bot.agents.context import AgentContext
        from tests.conftest import make_settings

        ctx = AgentContext(cfg, rig.ctx.registry, db, Bus(db, "assistant", owner_id=ADMIN_ID))
        llm = FakeGroq()
        agent = GenericAgent(ctx, llm, make_settings(tmp_path))
        llm.replies.append(json.dumps({"action": "call_owner", "text": "срочно проверьте сервер", "urgent": True}))
        answer = await agent.answer("позвони мне, если упадёт сервер")
        assert answer == "📞 Попросил Кимо позвонить вам."
        assert '"call_owner"' in llm.prompts[0][0]["content"]  # действие доступно агентам
        await asyncio.sleep(0.5)
        assert len(rig.stack.calls) == 1
        rig.stack.handlers[0].on_ended("bye")
        # агент может быть лишён права звонить флагом в конфиге
        await db.update_agent("assistant", config={**cfg.config, "can_call_owner": False})
        ctx2 = AgentContext(await rig.ctx.registry.get("assistant"), rig.ctx.registry, db, ctx.bus)
        agent2 = GenericAgent(ctx2, FakeGroq(), make_settings(tmp_path))
        agent2.llm.replies.append(json.dumps({"action": "call_owner", "text": "ещё раз"}))
        assert "не разрешено" in await agent2.answer("позвони")
    finally:
        await close(rig)
