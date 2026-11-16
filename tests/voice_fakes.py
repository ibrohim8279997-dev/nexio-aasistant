"""Подмены для проверки логики звонка без pjsua2: звонок, стек и «клиент» SIP, считающий digest как Linphone."""
from __future__ import annotations

import hashlib
from collections import deque

from bot.voice.digest import IncomingGate, parse_digest
from bot.voice.stack import FRAME_BYTES, FRAME_MS, split_frames


def md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def client_authorization(challenge: str, user: str, password: str, method: str = "INVITE",
                         uri: str = "sip:kimo@100.64.0.1:5060", nc: int = 1, cnonce: str = "abc123") -> str:
    """Заголовок Authorization по вызову WWW-Authenticate — так считает любой SIP-клиент (RFC 2617)."""
    p = parse_digest(challenge)
    ha1 = md5(f"{user}:{p['realm']}:{password}")
    ha2 = md5(f"{method}:{uri}")
    ncs = f"{nc:08x}"
    response = md5(f"{ha1}:{p['nonce']}:{ncs}:{cnonce}:auth:{ha2}")
    return (f'Digest username="{user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="{uri}", '
            f'response="{response}", algorithm=MD5, cnonce="{cnonce}", qop=auth, nc={ncs}')


class FakeSipCall:
    def __init__(self, call_id: str = "1", remote: str = "100.64.0.9", direction: str = "in") -> None:
        self.call_id, self.remote, self.direction = call_id, remote, direction
        self.out: deque[bytes] = deque()
        self.sent_pcm = b""
        self.hung_up = False
        self.cleared = 0

    def send_audio(self, pcm: bytes) -> None:
        self.sent_pcm += pcm
        self.out.extend(split_frames(pcm))

    def clear_audio(self) -> None:
        self.out.clear()
        self.cleared += 1

    def pending_ms(self) -> int:
        return len(self.out) * FRAME_MS

    def hangup(self) -> None:
        self.hung_up = True

    def play(self, ms: int) -> None:
        """Имитация воспроизведения: pjsip забирает кадры из очереди."""
        for _ in range(ms // FRAME_MS):
            if self.out:
                self.out.popleft()


class FakeStack:
    """Принимает «INVITE» так же, как адаптер pjsua2: через IncomingGate; создаёт обработчик только при коде 200."""

    def __init__(self, gate: IncomingGate, on_incoming, loop=None) -> None:
        self.gate, self.on_incoming = gate, on_incoming
        self.calls: list[FakeSipCall] = []
        self.handlers = []
        self.outgoing: list[tuple[str, object]] = []

    def invite(self, src_ip: str, authorization: str | None = None):
        decision = self.gate.decide(src_ip, "INVITE", authorization)
        if decision.code == 200:
            call = FakeSipCall(str(len(self.calls) + 1), src_ip)
            self.calls.append(call)
            handler = self.on_incoming(call)
            self.handlers.append(handler)
            handler.on_connected()
            return decision, call, handler
        return decision, None, None

    def call_out(self, uri: str, handler):
        call = FakeSipCall(str(len(self.calls) + 1), uri, "out")
        self.calls.append(call)
        self.outgoing.append((uri, handler))
        self.handlers.append(handler)
        return call


def silence(ms: int) -> list[bytes]:
    return [b"\x00" * FRAME_BYTES for _ in range(ms // FRAME_MS)]
