"""Этап 3: звонок <-> Кимо через очередь. Задание voice_command с chat_id владельца, ответ озвучивается,
audit_log, права не меняются (голосовое «да» ничего не подтверждает), текст в Telegram идёт через Кимо."""
import asyncio
import json

import pytest

from bot.agents import procs
from bot.agents.access import ScopedDB
from bot.agents.admin_actions import AdminActions
from bot.agents.admin_ops import AdminOps
from bot.agents.bus import Bus, BusError, BusWorker
from bot.agents.confirm import Confirmations
from bot.agents.context import AgentContext
from bot.agents.health import AgentState
from bot.agents.kimo import KimoBusHandler
from bot.agents.registry import Registry
from bot.timeutil import now_utc, to_db
from bot.voice.kimo_link import BusKimoLink, BusRelay, parse_result
from bot.voice.service import VoiceService
from bot.voice.session import CallSession, VoiceDeps
from bot.voice.tg import voice_reply
from bot.voice.tts import Synthesizer
from bot.voice.vad import EnergyVad
from tests.conftest import ADMIN_ID, FakeGroq, FakeRates, make_settings
from tests.fakebot import FakeBot
from tests.test_voice_stage2 import FakeAsr, FakeTts, cfg_for, say_phrase
from tests.voice_fakes import FakeSipCall

NOW = "2026-11-10 10:00:00"


class Rig:
    """Кимо (настоящий обработчик шины с FakeGroq) + служба звонков (настоящий BusKimoLink)."""

    async def build(self, db, tmp_path, wait=2.0, late=3.0):
        self.db = db
        self.settings = make_settings(tmp_path)
        reg = Registry(db)
        await reg.seed_defaults()
        for a in await reg.all():
            await db.agent_heartbeat(a.name, "online", to_db(now_utc()))
        cfg = await reg.get("kimo")
        self.kimo_bus = Bus(db, "kimo", owner_id=ADMIN_ID)
        self.ctx = AgentContext(cfg, reg, db, self.kimo_bus)
        self.scoped = ScopedDB(db, "kimo", cfg.allowed_tables, "admin", live=True)
        await self.scoped.ensure_user(ADMIN_ID, NOW)
        self.bot = FakeBot()
        ops = AdminOps(db, "kimo", tmp_path / "logs")
        self.confirm = Confirmations(db, "kimo", ops, ADMIN_ID, online_timeout=1.0)
        self.ctx.admin = AdminActions(self.ctx, ops, self.confirm, self.bot)
        self.llm = FakeGroq()
        self.handler = KimoBusHandler(self.ctx, self.scoped, self.llm, FakeRates(self.scoped), self.settings, self.bot)
        self.worker = BusWorker(self.kimo_bus, self.handler, self.bot, ADMIN_ID, interval=0.05)
        self.runner = asyncio.create_task(self.worker.run())
        self.voice_bus = Bus(db, "voice_sip", owner_id=ADMIN_ID)
        self.cfg = cfg_for(answer_wait_seconds=int(wait))
        self.state = AgentState()
        self.service = VoiceService(self.cfg, db, self.state, lambda c, s: None)
        self.relay = BusRelay(self.voice_bus, ADMIN_ID)
        self.link = BusKimoLink(self.voice_bus, db, self.relay, self.service, wait, ADMIN_ID, late)
        return self

    async def close(self):
        self.worker.stop()
        await self.runner


@pytest.fixture
async def rig(db, tmp_path):
    r = await Rig().build(db, tmp_path)
    yield r
    await r.close()


def actions(*items):
    return json.dumps({"actions": list(items)})


async def test_russian_and_uzbek_phrases_become_tasks_for_kimo(rig):
    rig.llm.replies += [
        actions({"type": "add_task", "text": "Купить хлеб", "date": None, "time": "23:59"}),
        actions({"type": "reply", "text": "Qabul qilindi."}),
    ]
    ru = await rig.link.ask("добавь задачу купить хлеб сегодня в 23:59", [], "c1")
    uz = await rig.link.ask("ertaga soat o'nda uchrashuv bor", [("добавь задачу", "Готово")], "c1")
    assert not ru.failed and "Задача добавлена" in ru.text and uz.text == "Qabul qilindi."
    tasks = await rig.db._all("SELECT * FROM tasks_queue WHERE from_agent = 'voice_sip' ORDER BY id")
    assert len(tasks) == 2
    for row, phrase in zip(tasks, ("добавь задачу купить хлеб сегодня в 23:59", "ertaga soat o'nda uchrashuv bor")):
        payload = json.loads(row["payload"])
        assert row["to_agent"] == "kimo" and row["depth"] == 1 and row["chat_id"] == ADMIN_ID  # чат владельца, ответ не потеряется
        assert payload["type"] == "voice_command" and payload["source"] == "voice_call" and payload["text"] == phrase
        assert payload["context"]["from"] == "voice_sip" and payload["context"]["call"] == "c1"
        assert row["status"] == "done" and row["delivered"] == 1
    owner = await rig.scoped.ensure_user(ADMIN_ID, NOW)
    assert [t["text"] for t in await rig.scoped.open_tasks(owner["id"])] == ["Купить хлеб"]  # действие реально выполнено
    assert "Предыдущие реплики" in rig.llm.prompts[1][0]["content"]  # история звонка дошла до Кимо
    assert "по телефону" in rig.llm.prompts[0][0]["content"]  # подсказка отвечать коротко


async def test_voice_has_owner_rights_same_as_telegram(rig):
    """Голос — ввод владельца: расходы доступны (из шины для других агентов закрыты)."""
    rig.llm.replies.append(actions({"type": "add_expense", "amount": 25000, "currency": "UZS", "category": "такси"}))
    reply = await rig.link.ask("такси двадцать пять тысяч", [], "c2")
    assert not reply.failed and "25 000" in reply.text
    assert len(await rig.scoped.expenses_between(1, "2000-01-01 00:00:00", "2100-01-01 00:00:00")) == 1


async def test_audit_log_phrase_and_answering_agent(rig):
    async def assistant_handler(task):
        return "Столица Узбекистана — Ташкент"

    asst = BusWorker(Bus(rig.db, "assistant"), assistant_handler, FakeBot(), ADMIN_ID, interval=0.05)
    asst_runner = asyncio.create_task(asst.run())
    try:
        rig.llm.replies.append(actions({"type": "delegate", "agent": "Мими", "task": "столица Узбекистана?"}))
        reply = await rig.link.ask("спроси у Мими какая столица Узбекистана", [], "c3")
    finally:
        asst.stop()
        await asst_runner
    assert "Ташкент" in reply.text
    rows = await rig.db.audit_list(action="voice_phrase")
    entry = json.loads(rows[0]["details"])
    assert entry["text"] == "спроси у Мими какая столица Узбекистана" and entry["answered_by"] == ["kimo", "assistant"]
    assert rows[0]["actor"] == "voice_sip" and rows[0]["ts"] and entry["ok"] is True


async def test_voice_yes_never_confirms_dangerous_action(rig):
    """7: голосовое «да» не подтверждает остановку агента и прочее: подтверждение только текстом в Telegram."""
    await rig.db.insert_agent({"name": "midi", "display_name": "Миди", "role": "агент встреч", "enabled": True,
                               "created_by": "agent_builder"}, NOW)
    stops = []

    class Control:
        async def start(self, name):
            return True, "ok"

        async def stop(self, name):
            stops.append(name)

    procs.set_control(Control())
    try:
        proposal = await rig.confirm.propose("stop", "midi")  # Кимо уже предложил остановку, ждёт «да» в Telegram
        rig.llm.replies.append(actions({"type": "reply", "text": "Подтверждение отправлено вам в Telegram."}))
        await rig.link.ask("да", [], "c4")
        rig.llm.replies.append(actions({"type": "manage_agent", "agent": "Миди", "op": "stop"}))
        reply2 = await rig.link.ask("да, останови Миди", [("останови Миди", "Подтвердите")], "c4")
    finally:
        procs.set_control(procs.TmuxControl())
    assert stops == []  # ничего не остановлено
    row = await rig.db.pending_get(proposal.id)
    assert row["status"] == "new"  # прежний запрос так и ждёт Telegram
    open_ids = {p["id"] for p in await rig.confirm.open_pending()}
    assert proposal.id in open_ids and len(open_ids) == 2  # голос создал ещё один запрос на подтверждение, а не выполнил
    assert "Telegram" in reply2.text or "личку" in reply2.text
    assert (await rig.db.get_agent("midi"))["enabled"] is True
    # и сама процедура подтверждения голосу недоступна: только владелец в личке Telegram
    from bot.agents.confirm import ConfirmError
    with pytest.raises(ConfirmError):
        await rig.confirm.decide(proposal.id, True, user_id=ADMIN_ID, private=False)


async def test_only_voice_service_may_send_voice_commands_or_tg_messages(rig):
    for sender in ("assistant", "agent_builder"):
        bus = Bus(rig.db, sender, owner_id=ADMIN_ID)
        with pytest.raises(BusError):
            await bus.request("kimo", {"type": "voice_command", "source": "voice_call", "text": "удали всё"}, timeout=3)
        with pytest.raises(BusError):
            await bus.request("kimo", {"type": "tg_message", "text": "спам"}, timeout=3)
    # voice_sip без source=voice_call тоже не проходит
    with pytest.raises(BusError):
        await rig.voice_bus.request("kimo", {"type": "voice_command", "text": "привет"}, timeout=3)


async def test_kimo_sends_text_to_owner_via_tg_message(rig):
    await rig.relay.send("📞 Подробный отчёт\n" + "строка\n" * 5)
    for _ in range(40):
        if rig.bot.calls:
            break
        await asyncio.sleep(0.05)
    msg = rig.bot.calls[-1]
    assert msg.chat_id == ADMIN_ID and "Подробный отчёт" in msg.text


async def test_kimo_offline_fails_fast_without_task(rig):
    await rig.db._write("UPDATE agents SET last_seen = '2020-01-01 00:00:00' WHERE name = 'kimo'")
    reply = await rig.link.ask("привет", [], "c5")
    assert reply.failed and "недоступен" in reply.text
    assert await rig.db._all("SELECT 1 FROM tasks_queue WHERE from_agent = 'voice_sip'") == []
    assert json.loads((await rig.db.audit_list(action="voice_phrase"))[0]["details"])["ok"] is False


async def test_kimo_failure_reason_returned(rig):
    rig.llm.replies.append("не json вообще")
    reply = await rig.link.ask("абракадабра", [], "c6")
    assert reply.failed and "не понял" in reply.text


async def test_late_answer_is_sent_to_telegram_not_lost(db, tmp_path):
    rig = await Rig().build(db, tmp_path, wait=0.3, late=5.0)
    try:
        rig.worker.stop()
        await rig.runner  # Кимо временно не берёт задания
        rig.llm.replies.append(actions({"type": "reply", "text": "Позже, но ответ есть."}))
        reply = await rig.link.ask("вопрос", [], "c7")
        assert reply.timed_out and not reply.failed
        rig.worker = BusWorker(rig.kimo_bus, rig.handler, rig.bot, ADMIN_ID, interval=0.05)
        rig.runner = asyncio.create_task(rig.worker.run())  # Кимо «ожил»: задание выполнено после тайм-аута
        for _ in range(100):
            await asyncio.sleep(0.1)
            if any("Позже, но ответ есть." in (getattr(c, "text", "") or "") for c in rig.bot.calls):
                break
        texts = [getattr(c, "text", "") or "" for c in rig.bot.calls]
        assert any("пришёл позже" in t and "Позже, но ответ есть." in t for t in texts)
    finally:
        await rig.close()


async def test_full_call_session_with_real_kimo_link(rig):
    rig.llm.replies.append(actions({"type": "reply", "text": "Всё готово."}))
    call, synth = FakeSipCall("9"), FakeTts()
    deps = VoiceDeps(FakeAsr(["сделай отчёт"]), synth, rig.link, rig.relay, rig.cfg)
    service = VoiceService(rig.cfg, rig.db, rig.state, lambda c, s: None)
    session = CallSession(call, service, deps, vad=EnergyVad())

    async def player():
        while True:
            call.play(100)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(player())
    session.on_connected()
    await asyncio.sleep(0.3)
    await say_phrase(session)
    await asyncio.sleep(1.5)
    assert ("Всё готово.", "ru") in synth.spoken  # ответ Кимо озвучен в звонок
    session.on_ended("200 OK")
    await asyncio.sleep(1.0)  # расшифровка звонка уходит в Telegram через Кимо
    task.cancel()
    texts = [getattr(c, "text", "") or "" for c in rig.bot.calls]
    assert any("— Вы: сделай отчёт" in t and "— Кимо: Всё готово." in t for t in texts)


def test_parse_result_json_and_plain():
    assert parse_result('{"text": "ок", "answered_by": ["kimo", "assistant"]}') == ("ок", ["kimo", "assistant"])
    assert parse_result("просто текст") == ("просто текст", ["kimo"])


class FakeMessage:
    def __init__(self):
        self.voices = []

    async def answer_voice(self, voice):
        self.voices.append(voice)


async def test_telegram_voice_reply_is_ogg_opus_and_optional():
    async def source(text, voice):
        import subprocess

        return subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                               "-f", "mp3", "pipe:1"], capture_output=True, check=True).stdout

    msg = FakeMessage()
    assert await voice_reply(msg, "Задача добавлена на завтра.", Synthesizer(source=source))
    assert msg.voices[0].data.startswith(b"OggS")  # ogg opus для голосовых сообщений Telegram
    assert not await voice_reply(msg, "текст", None) and len(msg.voices) == 1  # VOICE_TG_REPLY=false: только текст
