"""Этап 1: приём звонка. Пароль (digest), 403 при неверном, блокировка после 5 попыток, один звонок за раз, журнал."""
import asyncio
import logging

import pytest

from bot.agents.health import AgentState
from bot.config import ConfigError
from bot.voice.config import VoiceConfig
from bot.voice.digest import AttemptGuard, AuthResult, DigestAuthenticator, IncomingGate, extract_authorization
from bot.voice.service import LoggingSession, VoiceService
from tests.voice_fakes import FakeStack, client_authorization

USER, PASSWORD = "owner", "correct-horse-battery"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make_gate(clock=None, busy=lambda: False):
    clock = clock or Clock()
    return IncomingGate(DigestAuthenticator(USER, PASSWORD, "kimo", clock=clock, secret=b"s" * 32),
                        AttemptGuard(clock=clock), busy), clock


def linphone_call(gate, password=PASSWORD, user=USER, src="100.64.0.9"):
    """Как делает Linphone: INVITE без пароля -> 401 с вызовом -> INVITE с Authorization."""
    first = gate.decide(src, "INVITE", None)
    assert first.code == 401 and first.www_authenticate
    return gate.decide(src, "INVITE", client_authorization(first.www_authenticate, user, password))


def test_correct_password_accepted_wrong_rejected_403():
    gate, _ = make_gate()
    assert linphone_call(gate).code == 200
    wrong = linphone_call(gate, password="wrong-password-123", src="100.64.0.10")
    assert wrong.code == 403
    assert linphone_call(gate, user="intruder", src="100.64.0.11").code == 403


def test_nonce_replay_and_expiry():
    gate, clock = make_gate()
    first = gate.decide("100.64.0.9", "INVITE", None)
    header = client_authorization(first.www_authenticate, USER, PASSWORD)
    assert gate.decide("100.64.0.9", "INVITE", header).code == 200
    assert gate.decide("100.64.0.9", "INVITE", header).code == 403  # записанный запрос повторить нельзя
    old = gate.decide("100.64.0.9", "INVITE", None)
    clock.t += 500  # nonce устарел: просто новый вызов, а не подбор пароля
    assert gate.decide("100.64.0.9", "INVITE", client_authorization(old.www_authenticate, USER, PASSWORD)).code == 401


def test_five_failures_block_address_for_15_minutes():
    gate, clock = make_gate()
    for i in range(5):
        assert linphone_call(gate, password=f"guess-number-{i}-xx").code == 403
    # адрес заблокирован: даже верный пароль не помогает
    assert gate.decide("100.64.0.9", "INVITE", None).code == 403
    assert linphone_call_blocked(gate)
    clock.t += 15 * 60 + 1
    assert linphone_call(gate).code == 200  # блокировка снята
    # другой адрес не пострадал
    assert linphone_call(make_gate()[0], src="100.64.0.77").code == 200


def linphone_call_blocked(gate):
    d = gate.decide("100.64.0.9", "INVITE", "Digest username=\"owner\"")
    return d.code == 403 and "заблокирован" in d.reason


def test_success_resets_failure_counter():
    gate, _ = make_gate()
    for _ in range(4):
        assert linphone_call(gate, password="bad-password-xx").code == 403
    assert linphone_call(gate).code == 200
    for _ in range(4):
        assert linphone_call(gate, password="bad-password-xx").code == 403
    assert linphone_call(gate).code == 200  # 4+4 неудач не подряд: не заблокирован


def test_busy_line_returns_486_only_after_auth():
    gate, _ = make_gate(busy=lambda: True)
    assert gate.decide("100.64.0.9", "INVITE", None).code == 401  # занятость постороннему не раскрывается
    assert linphone_call(gate).code == 486


def test_extract_authorization_from_sip_message():
    msg = ("INVITE sip:kimo@100.64.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP x\r\n"
           'Authorization: Digest username="owner", realm="kimo"\r\nContent-Length: 0\r\n\r\n')
    assert extract_authorization(msg).startswith("Digest username=")
    assert extract_authorization("INVITE sip:x SIP/2.0\r\n\r\n") is None
    auth = DigestAuthenticator(USER, PASSWORD)
    assert auth.verify("INVITE", None) is AuthResult.NO_CREDENTIALS and auth.verify("INVITE", "Basic abc") is AuthResult.BAD


def env(monkeypatch, **over):
    values = {"SIP_USER": USER, "SIP_PASSWORD": PASSWORD, "SIP_BIND_ADDRESS": "100.119.11.24"}
    values.update(over)
    for key in ("SIP_USER", "SIP_PASSWORD", "SIP_BIND_ADDRESS", "SIP_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_config_requires_tailscale_address_and_strong_password(monkeypatch):
    env(monkeypatch)
    cfg = VoiceConfig.from_env()
    assert cfg.bind_address == "100.119.11.24" and cfg.record_calls is False  # запись звонков по умолчанию выключена
    for bad in ("0.0.0.0", "192.168.1.5", "8.8.8.8", "not-an-ip"):
        env(monkeypatch, SIP_BIND_ADDRESS=bad)
        with pytest.raises(ConfigError):
            VoiceConfig.from_env()
    env(monkeypatch, SIP_PASSWORD="short")
    with pytest.raises(ConfigError):
        VoiceConfig.from_env()
    env(monkeypatch, SIP_USER="")
    with pytest.raises(ConfigError):
        VoiceConfig.from_env()


async def test_call_accepted_logged_and_audited(db, caplog, monkeypatch):
    env(monkeypatch)
    cfg = VoiceConfig.from_env()
    state = AgentState()
    service = VoiceService(cfg, db, state, LoggingSession)
    gate, _ = make_gate(busy=service.busy)
    stack = FakeStack(gate, service.on_incoming)

    # неверный пароль: звонок не принят, обработчик не создан
    first = gate.decide("100.64.0.9", "INVITE", None)
    decision, call, handler = stack.invite("100.64.0.9", client_authorization(first.www_authenticate, USER, "wrong-password-1"))
    assert decision.code == 403 and call is None and not service.busy()

    with caplog.at_level(logging.INFO, logger="voice.calls"):
        first = gate.decide("100.64.0.8", "INVITE", None)
        decision, call, handler = stack.invite("100.64.0.8", client_authorization(first.www_authenticate, USER, PASSWORD))
        assert decision.code == 200 and service.busy() and state.busy_tasks == 1  # heartbeat покажет busy
        # второй звонок во время разговора: занято
        assert gate.decide("100.64.0.7", "INVITE", None).code == 401
        second = gate.decide("100.64.0.7", "INVITE", None)
        assert gate.decide("100.64.0.7", "INVITE", client_authorization(second.www_authenticate, USER, PASSWORD)).code == 486
        handler.on_ended("200 Normal call clearing")
    assert not service.busy() and state.busy_tasks == 0
    line = next(r.getMessage() for r in caplog.records if "Звонок завершён" in r.getMessage())
    assert "длительность=" in line and "причина=200 Normal call clearing" in line and "направление=in" in line
    assert PASSWORD not in caplog.text
    await asyncio.sleep(0.05)  # запись в audit_log идёт в фоне
    rows = await db.audit_list(action="voice_call")
    assert rows and rows[0]["actor"] == "voice_sip" and '"duration_s"' in rows[0]["details"]


async def test_second_call_while_busy_is_hung_up(db, monkeypatch):
    env(monkeypatch)
    service = VoiceService(VoiceConfig.from_env(), db, AgentState(), LoggingSession)
    from tests.voice_fakes import FakeSipCall

    first, second = FakeSipCall("1"), FakeSipCall("2")
    service.on_incoming(first)
    service.on_incoming(second)  # гонка двух INVITE: вторую линия не берёт
    assert second.hung_up and not first.hung_up
