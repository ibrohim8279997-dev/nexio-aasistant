"""Этап 2: конец фразы (VAD), Whisper, edge-tts (голос по языку ответа), перебивание, сбои без молчания."""
import asyncio
import io
import logging
import math
import shutil
import struct
import subprocess
import wave
from dataclasses import replace

import pytest

from bot.agents.health import AgentState
from bot.ai.groq_client import GroqUnavailableError
from bot.voice import tts
from bot.voice.asr import Recognizer, SpeechUnavailable, clean_transcript, pcm_to_wav
from bot.voice.config import VoiceConfig
from bot.voice.service import VoiceService
from bot.voice.session import CallSession, KimoReply, VoiceDeps
from bot.voice.stack import FRAME_BYTES, FRAME_MS, RATE
from bot.voice.tts import Synthesizer, TtsUnavailable, detect_lang, pick_voice, spoken
from bot.voice.vad import EnergyVad, Segmenter, SpeechStart, Utterance, make_vad
from tests.voice_fakes import FakeSipCall, silence


def tone(ms: int, hz: int = 300, amp: int = 6000) -> list[bytes]:
    frames = []
    for f in range(ms // FRAME_MS):
        base = f * FRAME_BYTES // 2
        samples = [int(amp * math.sin(2 * math.pi * hz * (base + i) / RATE)) for i in range(FRAME_BYTES // 2)]
        frames.append(struct.pack(f"<{len(samples)}h", *samples))
    return frames


def segment(frames):
    seg = Segmenter(EnergyVad())
    events = []
    for frame in frames:
        events.extend(seg.feed(frame))
    return events


# ---------- VAD ----------

def test_pause_of_800ms_ends_phrase():
    events = segment(silence(300) + tone(1000) + silence(1000))
    assert [type(e) for e in events] == [SpeechStart, Utterance]
    assert 900 <= events[1].speech_ms <= 1100


def test_short_noise_under_400ms_is_not_sent():
    assert [e for e in segment(silence(200) + tone(300) + silence(1200)) if isinstance(e, Utterance)] == []


def test_short_pause_inside_phrase_does_not_split():
    events = segment(tone(700) + silence(500) + tone(700) + silence(1000))
    assert len([e for e in events if isinstance(e, Utterance)]) == 1


def test_long_speech_is_cut_at_max_length():
    events = segment(tone(20000))
    assert any(isinstance(e, Utterance) for e in events)


def test_silence_never_triggers_and_make_vad_works():
    assert segment(silence(3000)) == []
    vad = make_vad()
    assert vad(b"\x00" * FRAME_BYTES) is False


# ---------- Whisper ----------

def test_wav_is_16k_mono():
    pcm = b"".join(tone(500))
    with wave.open(io.BytesIO(pcm_to_wav(pcm))) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == len(pcm) // 2


@pytest.mark.parametrize("text", ["Субтитры сделал DimaTorzok", "Продолжение следует...", "Thanks for watching!", "...", " ", None, "я"])
def test_noise_and_hallucinations_dropped(text):
    assert clean_transcript(text) is None


class FakeGroq:
    def __init__(self, texts=None, error=None):
        self.texts, self.error, self.calls = list(texts or []), error, []

    async def transcribe(self, audio, filename="voice.ogg"):
        self.calls.append((audio[:4], filename))
        if self.error:
            raise self.error
        return self.texts.pop(0)


async def test_russian_and_uzbek_phrases_recognized():
    groq = FakeGroq(["Добавь задачу купить хлеб завтра", "Ertaga soat o'nda uchrashuv bor"])
    asr = Recognizer(groq)
    assert await asr.recognize(b"\x01\x00" * 800) == "Добавь задачу купить хлеб завтра"
    assert await asr.recognize(b"\x01\x00" * 800) == "Ertaga soat o'nda uchrashuv bor"
    assert groq.calls[0] == (b"RIFF", "phrase.wav")  # язык не фиксируется, уходит обычный wav


async def test_groq_down_raises_speech_unavailable_and_logs(caplog):
    asr = Recognizer(FakeGroq(error=GroqUnavailableError("auth")))
    with caplog.at_level(logging.ERROR, logger="voice.asr"), pytest.raises(SpeechUnavailable):
        await asr.recognize(b"\x01\x00" * 800)
    assert "Whisper недоступен" in caplog.text


# ---------- озвучка ----------

def test_language_by_reply_text_not_by_input():
    assert detect_lang("Задача добавлена на завтра") == "ru"
    assert detect_lang("Vazifa qo'shildi: ertaga soat 10 da uchrashuv") == "uz"
    assert detect_lang("Salom, bugun nima qilamiz?") == "uz"
    assert detect_lang("Task added for tomorrow") == "en"
    assert detect_lang("12345") == "ru"


def test_voice_selection():
    assert pick_voice("ru") == "ru-RU-SvetlanaNeural" and pick_voice("ru", ru="dmitry") == "ru-RU-DmitryNeural"
    assert pick_voice("uz") == "uz-UZ-MadinaNeural" and pick_voice("uz", uz="sardor") == "uz-UZ-SardorNeural"
    assert pick_voice("en") == "ru-RU-SvetlanaNeural"


async def test_synthesizer_uses_voice_of_reply_language():
    seen = []

    async def source(text, voice):
        seen.append(voice)
        return b"mp3"

    s = Synthesizer("dmitry", "sardor", source)
    await s.mp3("Привет", "ru")
    await s.mp3("Salom", "uz")
    assert seen == ["ru-RU-DmitryNeural", "uz-UZ-SardorNeural"]


def test_spoken_short_long_and_structured():
    short = spoken("Задача добавлена на завтра в 10:00.")
    assert short.text == "Задача добавлена на завтра в 10:00." and not short.details
    table = spoken("Расходы:\n• еда 10\n• такси 5\n• кофе 3\n• аренда 100\nИтого 118")
    assert table.text == "Подробности отправил в чат." and table.details
    assert spoken("Смотри https://example.com/report подробно").details
    assert spoken("<pre>2026-11-10 ERROR bot: упал\nTraceback...</pre>").text == "Подробности отправил в чат."
    long = spoken("Главный вывод такой. " + "Подробный текст. " * 60)
    assert long.details and long.text == "Главный вывод такой. Подробности отправил в чат."
    uz = spoken("Batafsil: " + "ma'lumot va natija bilan uchun " * 30)
    assert uz.details and uz.text.endswith("Batafsil ma'lumotni chatga yubordim.")
    assert spoken("<b>Готово</b> ✅ задача 🙂").text == "Готово задача"  # без разметки и эмодзи


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="нужен ffmpeg")
async def test_ffmpeg_formats_wav16k_and_ogg_opus():
    mp3 = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                          "-f", "mp3", "pipe:1"], capture_output=True, check=True).stdout
    pcm = await tts.to_pcm(mp3)
    assert 30000 <= len(pcm) <= 34500  # 1 с, 16 кГц, 16 бит, моно
    ogg = await tts.to_ogg_opus(mp3)
    assert ogg.startswith(b"OggS") and b"OpusHead" in ogg[:200]
    with pytest.raises(TtsUnavailable):
        await tts.to_pcm(b"not audio")


async def test_edge_tts_missing_or_down_is_tts_unavailable(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "edge_tts", None)
    with pytest.raises(TtsUnavailable):
        await tts.edge_mp3("привет", "ru-RU-SvetlanaNeural")


# ---------- сессия звонка ----------

class FakeAsr:
    def __init__(self, texts=None, error=None):
        self.texts, self.error = list(texts or ["привет"]), error

    async def recognize(self, pcm):
        if self.error:
            raise self.error
        return self.texts.pop(0) if self.texts else None


class FakeTts:
    def __init__(self, ms=400, fail=False, fallback=True, long_for=None):
        self.ms, self.fail, self.spoken, self.long_for = ms, fail, [], long_for or {}
        self._fallback = b"".join(tone(300, 500)) if fallback else None

    async def pcm(self, text, lang):
        self.spoken.append((text, lang))
        if self.fail:
            raise TtsUnavailable("edge-tts: ConnectionError")
        return b"".join(tone(self.long_for.get(text, self.ms))), tts.pick_voice(lang)

    def unavailable_pcm(self, lang="ru"):
        return self._fallback


class FakeKimo:
    def __init__(self, text="Готово.", delay=0.0, **kw):
        self.text, self.delay, self.kw, self.asked = text, delay, kw, []

    async def ask(self, text, history, call_id):
        self.asked.append((text, list(history)))
        await asyncio.sleep(self.delay)
        return KimoReply(self.text, **self.kw)


class FakeRelay:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def cfg_for(**over):
    base = VoiceConfig("owner", "pass-pass-1", 5060, "100.64.0.1", "", False, 900, 120, 20, 30, "svetlana", "madina")
    return replace(base, **over)


async def make_session(db, *, asr=None, tts_=None, kimo=None, cfg=None, realtime=False, **kw):
    service = VoiceService(cfg or cfg_for(), db, AgentState(), lambda c, s: None)
    call = FakeSipCall("7")
    relay = FakeRelay()
    deps = VoiceDeps(asr or FakeAsr(), tts_ or FakeTts(), kimo or FakeKimo(), relay, cfg or cfg_for())
    session = CallSession(call, service, deps, vad=EnergyVad(), **kw)

    async def player():  # воспроизведение: pjsip забирает кадры из очереди (быстрее реального времени или в реальном)
        while True:
            call.play(20 if realtime else 100)
            await asyncio.sleep(0.02 if realtime else 0.01)

    task = asyncio.create_task(player())
    session.on_connected()
    return session, call, relay, task


async def say_phrase(session, ms=1000):
    for frame in tone(ms) + silence(1000):
        session.on_audio(frame)
    await asyncio.sleep(0.4)


async def settle(session, seconds=1.0):
    await asyncio.sleep(seconds)


async def test_phrase_goes_to_kimo_and_reply_is_spoken_in_call(db):
    kimo = FakeKimo("Задача добавлена на завтра.")
    synth = FakeTts()
    session, call, relay, player = await make_session(db, asr=FakeAsr(["добавь задачу на завтра"]), tts_=synth, kimo=kimo)
    await asyncio.sleep(0.3)
    await say_phrase(session)
    await settle(session)
    assert kimo.asked == [("добавь задачу на завтра", [])]
    assert ("Слушаю.", "ru") in synth.spoken and ("Задача добавлена на завтра.", "ru") in synth.spoken
    assert len(call.sent_pcm) > 0
    session.on_ended("200 OK")
    await asyncio.sleep(0.1)
    player.cancel()
    assert relay.sent and "— Вы: добавь задачу на завтра" in relay.sent[0] and "— Кимо: Задача добавлена на завтра." in relay.sent[0]


async def test_uzbek_reply_gets_uzbek_voice(db):
    synth = FakeTts()
    session, call, relay, player = await make_session(db, asr=FakeAsr(["ertaga uchrashuv"]), tts_=synth,
                                                     kimo=FakeKimo("Vazifa qo'shildi: ertaga soat 10 da."))
    await asyncio.sleep(0.3)
    await say_phrase(session)
    await settle(session)
    assert synth.spoken[-1] == ("Vazifa qo'shildi: ertaga soat 10 da.", "uz")
    session.on_ended("bye")
    player.cancel()


async def test_barge_in_stops_playback(db):
    synth = FakeTts(long_for={"Длинный ответ.": 8000})  # ответ 8 секунд
    session, call, relay, player = await make_session(
        db, asr=FakeAsr(["вопрос"]), tts_=synth, kimo=FakeKimo("Длинный ответ."), realtime=True)
    await asyncio.sleep(0.7)  # приветствие «Слушаю.» проиграно
    await say_phrase(session)
    await asyncio.sleep(0.5)
    assert session.speaking and call.pending_ms() > 3000  # Кимо говорит длинный ответ
    before = call.cleared
    for frame in tone(400, 250):  # владелец перебивает
        session.on_audio(frame)
    await asyncio.sleep(0.4)
    assert call.cleared == before + 1 and call.pending_ms() == 0  # очередь воспроизведения сброшена
    assert not session.speaking and session.barge_ins == 1
    session.on_ended("bye")
    player.cancel()


async def test_groq_down_agent_says_cannot_answer_not_silent(db, caplog):
    session, call, relay, player = await make_session(db, asr=FakeAsr(error=SpeechUnavailable("GroqUnavailableError")))
    await asyncio.sleep(0.3)
    sent_before = len(call.sent_pcm)
    await say_phrase(session)
    await settle(session)
    assert len(call.sent_pcm) > sent_before  # заготовка «сейчас не могу ответить» прозвучала
    session.on_ended("bye")
    player.cancel()


async def test_tts_down_beeps_and_messages_telegram(db):
    session, call, relay, player = await make_session(db, tts_=FakeTts(fail=True, fallback=False), asr=FakeAsr(["привет"]))
    await asyncio.sleep(0.3)
    await say_phrase(session)
    await settle(session)
    assert len(call.sent_pcm) > 0  # сигнал ошибки вместо тишины
    assert any("Сейчас не могу ответить" in m for m in relay.sent)  # и текст в Telegram с причиной «в логе»
    session.on_ended("bye")
    player.cancel()


async def test_kimo_slow_says_wait_then_timeout_phrase(db):
    synth = FakeTts()
    session, call, relay, player = await make_session(
        db, asr=FakeAsr(["вопрос"]), tts_=synth, kimo=FakeKimo(delay=0.4), wait_notice_after=0.05)
    await asyncio.sleep(0.3)
    await say_phrase(session)
    await settle(session)
    assert ("Секунду.", "ru") in synth.spoken
    session.on_ended("bye")
    player.cancel()

    session2, call2, relay2, player2 = await make_session(
        db, asr=FakeAsr(["вопрос"]), tts_=FakeTts(), kimo=FakeKimo("", timed_out=True))
    await asyncio.sleep(0.3)
    await say_phrase(session2)
    await settle(session2)
    assert any("Не успел получить ответ" in t for t, _ in session2.deps.tts.spoken)
    session2.on_ended("bye")
    player2.cancel()


async def test_long_reply_sent_to_telegram_short_phrase_spoken(db):
    long_text = "Главное: всё выполнено. " + "Подробности отчёта. " * 40
    synth = FakeTts()
    session, call, relay, player = await make_session(db, asr=FakeAsr(["отчёт"]), tts_=synth, kimo=FakeKimo(long_text))
    await asyncio.sleep(0.3)
    await say_phrase(session)
    await settle(session)
    assert relay.sent and long_text in relay.sent[0]
    assert synth.spoken[-1][0] == "Главное: всё выполнено. Подробности отправил в чат."
    session.on_ended("bye")
    player.cancel()


async def test_idle_call_is_hung_up(db):
    session, call, relay, player = await make_session(db, cfg=cfg_for(idle_seconds=0), tts_=FakeTts(ms=100))
    await asyncio.sleep(1.5)
    assert call.hung_up
    session.on_ended("bye")
    player.cancel()


async def test_audio_not_recorded_by_default_and_recorded_with_flag(db, tmp_path):
    session, call, relay, player = await make_session(db, record_dir=tmp_path / "calls")
    await asyncio.sleep(0.2)
    await say_phrase(session, 400)
    session.on_ended("bye")
    player.cancel()
    assert not (tmp_path / "calls").exists()  # без флага звук на диск не пишется
    session2, call2, relay2, player2 = await make_session(db, cfg=cfg_for(record_calls=True), record_dir=tmp_path / "calls2")
    await asyncio.sleep(0.2)
    await say_phrase(session2, 400)
    session2.on_ended("bye")
    player2.cancel()
    files = list((tmp_path / "calls2").glob("*.wav"))
    assert len(files) == 1 and files[0].stat().st_size > 1000 and oct(files[0].stat().st_mode)[-3:] == "600"
