"""Исправления по первому запуску на сервере: проверочный скрипт SIP, повторы edge-tts, ffmpeg не найден, ошибки колбэков в логе."""
import asyncio
import importlib.util
import logging
import re
import socket
import sys
import threading
from pathlib import Path

import pytest

from bot.voice import tts
from bot.voice.digest import AttemptGuard, DigestAuthenticator, IncomingGate
from bot.voice.tts import TtsUnavailable
from tests.test_voice_stage2 import cfg_for

ROOT = Path(__file__).resolve().parent.parent


def load_probe():
    spec = importlib.util.spec_from_file_location("sip_probe", ROOT / "scripts" / "sip_probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sip_probe"] = module
    spec.loader.exec_module(module)
    return module


class FakeSipServer(threading.Thread):
    """Минимальный UDP-сервер, отвечающий через настоящий IncomingGate (как адаптер pjsua2)."""

    def __init__(self, gate, with_challenge=True, ignore_options=False):
        super().__init__(daemon=True)
        self.gate, self.with_challenge, self.ignore_options = gate, with_challenge, ignore_options
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.seen = []
        self.running = True

    def run(self):
        self.sock.settimeout(0.2)
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            text = data.decode()
            method = text.split(" ", 1)[0]
            self.seen.append(method)
            if method in ("ACK", "BYE"):
                continue
            via = re.search(r"(?im)^Via:.*$", text).group(0)
            frm = re.search(r"(?im)^From:.*$", text).group(0)
            to = re.search(r"(?im)^To:.*$", text).group(0)
            callid = re.search(r"(?im)^Call-ID:.*$", text).group(0)
            cseq = re.search(r"(?im)^CSeq:.*$", text).group(0)
            tag = ";tag=srv1" if ";tag=" not in to else ""
            if method == "OPTIONS":
                if self.ignore_options:
                    continue  # pjsua2 так и делает: молчит на OPTIONS
                status, extra = "200 OK", ""
            else:
                auth = re.search(r"(?im)^Authorization:[ \t]*(.+)$", text)
                decision = self.gate.decide("127.0.0.1", "INVITE", auth.group(1) if auth else None)
                status = {200: "200 OK", 401: "401 Unauthorized", 403: "403 Forbidden"}.get(decision.code, f"{decision.code} X")
                extra = f"WWW-Authenticate: {decision.www_authenticate}\r\n" if decision.www_authenticate and self.with_challenge else ""
            reply = f"SIP/2.0 {status}\r\n{via}\r\n{frm}\r\n{to}{tag}\r\n{callid}\r\n{cseq}\r\n{extra}Content-Length: 0\r\n\r\n"
            self.sock.sendto(reply.encode(), addr)

    def stop(self):
        self.running = False
        self.sock.close()


def make_server(cfg, **kw):
    gate = IncomingGate(DigestAuthenticator(cfg.sip_user, cfg.sip_password, cfg.realm), AttemptGuard(), lambda: False)
    server = FakeSipServer(gate, **kw)
    server.start()
    return server


def test_probe_script_passes_against_correct_server(capsys):
    probe = load_probe()
    server = make_server(cfg_for(sip_user="kimo_ai", sip_password="correct-horse-1"))
    try:
        cfg = cfg_for(sip_user="kimo_ai", sip_password="correct-horse-1", sip_port=server.port)
        assert probe.probe("127.0.0.1", cfg) is True
        import time

        time.sleep(0.3)  # BYE доходит до сервера после завершения проверки
    finally:
        server.stop()
    out = capsys.readouterr().out
    assert "INVITE без пароля: SIP/2.0 401" in out and "WWW-Authenticate" in out and "с верным паролем: SIP/2.0 200" in out
    assert "correct-horse-1" not in out  # пароль не печатается
    assert server.seen.count("INVITE") == 2 and "BYE" in server.seen


def test_probe_works_when_server_ignores_options(capsys):
    """pjsua2 не отвечает на OPTIONS: проверка всё равно идёт звонком и проходит."""
    probe = load_probe()
    server = make_server(cfg_for(sip_user="kimo_ai", sip_password="correct-horse-1"), ignore_options=True)
    try:
        cfg = cfg_for(sip_user="kimo_ai", sip_password="correct-horse-1", sip_port=server.port)
        assert probe.probe("127.0.0.1", cfg) is True
    finally:
        server.stop()
    out = capsys.readouterr().out
    assert "OPTIONS: ответа нет" in out and "не ошибка" not in out  # мягкая пометка «·», не крест
    assert "· OPTIONS" in out and "с верным паролем: SIP/2.0 200" in out


def test_realm_configurable_from_env(monkeypatch):
    from tests.test_voice_stage1 import env

    env(monkeypatch)
    monkeypatch.setenv("SIP_REALM", "100.119.11.24")
    from bot.voice.config import VoiceConfig

    assert VoiceConfig.from_env().realm == "100.119.11.24"
    monkeypatch.setenv("SIP_REALM", 'bad"realm')
    import pytest as _pytest

    from bot.config import ConfigError

    with _pytest.raises(ConfigError):
        VoiceConfig.from_env()
    monkeypatch.delenv("SIP_REALM")


def test_probe_script_detects_missing_challenge(capsys):
    probe = load_probe()
    server = make_server(cfg_for(sip_user="kimo_ai", sip_password="correct-horse-1"), with_challenge=False)
    try:
        assert probe.probe("127.0.0.1", cfg_for(sip_user="kimo_ai", sip_password="correct-horse-1", sip_port=server.port)) is False
    finally:
        server.stop()
    assert "НЕТ заголовка WWW-Authenticate" in capsys.readouterr().out


def test_probe_script_reports_wrong_password(capsys):
    probe = load_probe()
    server = make_server(cfg_for(sip_user="kimo_ai", sip_password="correct-horse-1"))
    try:
        assert probe.probe("127.0.0.1", cfg_for(sip_user="kimo_ai", sip_password="other-password-2", sip_port=server.port)) is False
    finally:
        server.stop()
    assert "403" in capsys.readouterr().out


class FakeCommunicate:
    attempts = 0
    fail_times = 2

    def __init__(self, text, voice):
        pass

    async def stream(self):
        FakeCommunicate.attempts += 1
        if FakeCommunicate.attempts <= FakeCommunicate.fail_times:
            raise RuntimeError("NoAudioReceived")
        yield {"type": "audio", "data": b"mp3data"}


async def test_edge_tts_retries_no_audio_received(monkeypatch):
    import types

    FakeCommunicate.attempts, FakeCommunicate.fail_times = 0, 2
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=FakeCommunicate))
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(tts.asyncio, "sleep", fast_sleep)
    assert await tts.edge_mp3("привет", "ru-RU-SvetlanaNeural") == b"mp3data"
    assert FakeCommunicate.attempts == 3
    FakeCommunicate.attempts, FakeCommunicate.fail_times = 0, 99
    with pytest.raises(TtsUnavailable) as info:
        await tts.edge_mp3("привет", "ru-RU-SvetlanaNeural")
    assert "RuntimeError" in str(info.value) and FakeCommunicate.attempts == tts.EDGE_ATTEMPTS


async def test_missing_ffmpeg_gives_actionable_message(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(TtsUnavailable) as info:
        await tts.to_pcm(b"audio")
    assert "apt install -y ffmpeg" in str(info.value)


def test_callback_exceptions_are_logged_not_lost(caplog):
    from bot.voice.pjsua_stack import _auth_summary, _safe

    @_safe
    def broken():
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR, logger="voice.sip"):
        assert broken() is None
    assert "Ошибка в колбэке pjsua2: broken" in caplog.text and "boom" in caplog.text
    assert _auth_summary(None) == "Authorization нет"
    summary = _auth_summary('Digest username="kimo_ai", realm="100.119.11.24", nonce="n", uri="sip:x", response="abcdef"')
    assert "kimo_ai" in summary and "100.119.11.24" in summary and "abcdef" not in summary  # хеш-ответ в лог не идёт
