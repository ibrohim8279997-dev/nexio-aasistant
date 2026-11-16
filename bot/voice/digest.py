"""Digest-аутентификация входящих SIP-звонков и защита от подбора пароля.

pjsua2 умеет digest только для исходящих запросов, поэтому входящие проверяем сами (RFC 2617/7616, MD5, qop=auth):
- первый INVITE без учётных данных получает 401 с вызовом (иначе Linphone не поймёт, что нужен пароль);
- неверные данные — сразу 403; пять неверных попыток подряд с одного адреса блокируют адрес на 15 минут;
- nonce подписан HMAC, живёт ограниченное время, повторное использование nc отвергается.
Пароль нигде не логируется.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger("voice.auth")

NONCE_TTL = 120.0
MAX_FAILURES = 5
BLOCK_SECONDS = 15 * 60
_PARAM_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))')


class AuthResult(Enum):
    OK = "ok"
    NO_CREDENTIALS = "no_credentials"
    BAD = "bad"


def parse_digest(header: str | None) -> dict[str, str] | None:
    """Строка Authorization -> словарь параметров, либо None, если это не Digest."""
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("digest"):
        return None
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _PARAM_RE.finditer(header[6:])}


def extract_authorization(whole_msg: str) -> str | None:
    """Заголовок Authorization из текста SIP-сообщения."""
    m = re.search(r"(?im)^Authorization:[ \t]*(.+)$", whole_msg or "")
    return m.group(1).strip() if m else None


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()  # digest SIP: MD5 по протоколу


class DigestAuthenticator:
    def __init__(self, user: str, password: str, realm: str = "kimo", ttl: float = NONCE_TTL,
                 clock: Callable[[], float] = time.time, secret: bytes | None = None) -> None:
        self._user = user
        self._password = password
        self.realm = realm
        self._ttl = ttl
        self._clock = clock
        self._secret = secret or os.urandom(32)
        self._seen: dict[str, int] = {}  # nonce -> последний nc

    def _sign(self, ts: str) -> str:
        return hmac.new(self._secret, ts.encode(), hashlib.sha256).hexdigest()[:32]

    def new_nonce(self) -> str:
        """Время + случайная часть + подпись: каждый вызов уникален, подделать nonce нельзя."""
        body = format(int(self._clock()), "x") + os.urandom(4).hex()
        return body + self._sign(body)

    def challenge(self) -> str:
        """Значение заголовка WWW-Authenticate."""
        return f'Digest realm="{self.realm}", nonce="{self.new_nonce()}", algorithm=MD5, qop="auth"'

    def _nonce_valid(self, nonce: str) -> bool:
        if len(nonce) < 42:
            return False
        body, sig = nonce[:-32], nonce[-32:]
        try:
            issued = int(body[:-8], 16)
        except ValueError:
            return False
        return hmac.compare_digest(sig, self._sign(body)) and 0 <= self._clock() - issued <= self._ttl

    def verify(self, method: str, authorization: str | None) -> AuthResult:
        if not authorization:
            return AuthResult.NO_CREDENTIALS
        params = parse_digest(authorization)
        if params is None:
            return AuthResult.BAD
        username, nonce, uri = params.get("username", ""), params.get("nonce", ""), params.get("uri", "")
        response, qop = params.get("response", ""), params.get("qop")
        if params.get("algorithm", "MD5").upper() != "MD5" or params.get("realm", self.realm) != self.realm:
            return AuthResult.BAD
        if not self._nonce_valid(nonce):
            return AuthResult.NO_CREDENTIALS  # nonce устарел или чужой: даём новый вызов, это не подбор пароля
        ha1 = _md5(f"{self._user}:{self.realm}:{self._password}")
        ha2 = _md5(f"{method.upper()}:{uri}")
        if qop:
            nc, cnonce = params.get("nc", ""), params.get("cnonce", "")
            if qop != "auth" or not nc or not cnonce:
                return AuthResult.BAD
            expected = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        else:
            nc = ""
            expected = _md5(f"{ha1}:{nonce}:{ha2}")
        user_ok = hmac.compare_digest(username.encode(), self._user.encode())
        pass_ok = hmac.compare_digest(response.encode(), expected.encode())
        if not (user_ok and pass_ok):
            return AuthResult.BAD
        if nc:  # повтор того же nc с тем же nonce — подмена записанного запроса
            try:
                counter = int(nc, 16)
            except ValueError:
                return AuthResult.BAD
            if counter <= self._seen.get(nonce, 0):
                return AuthResult.BAD
            self._seen[nonce] = counter
            self._prune()
        return AuthResult.OK

    def _prune(self) -> None:
        if len(self._seen) > 512:
            self._seen = {n: c for n, c in self._seen.items() if self._nonce_valid(n)}


class AttemptGuard:
    """Пять неверных попыток подряд с адреса — блокировка на 15 минут. Успешный вход сбрасывает счётчик."""

    def __init__(self, max_failures: int = MAX_FAILURES, block_seconds: float = BLOCK_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._max = max_failures
        self._block = block_seconds
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked(self, ip: str) -> bool:
        until = self._blocked_until.get(ip)
        if until is None:
            return False
        if self._clock() >= until:
            del self._blocked_until[ip]
            self._failures.pop(ip, None)
            return False
        return True

    def failure(self, ip: str) -> bool:
        """Записать неудачу. True — адрес только что заблокирован."""
        count = self._failures.get(ip, 0) + 1
        self._failures[ip] = count
        if count >= self._max:
            self._blocked_until[ip] = self._clock() + self._block
            log.warning("Адрес %s заблокирован на %d мин: %d неверных попыток подряд", ip, int(self._block // 60), count)
            return True
        return False

    def success(self, ip: str) -> None:
        self._failures.pop(ip, None)


@dataclass(frozen=True)
class GateDecision:
    code: int  # SIP-код ответа: 200, 401, 403, 486
    www_authenticate: str | None = None
    reason: str = ""


class IncomingGate:
    """Решение по входящему INVITE: принять, потребовать пароль, отклонить 403, занято 486."""

    def __init__(self, auth: DigestAuthenticator, guard: AttemptGuard, busy: Callable[[], bool]) -> None:
        self.auth = auth
        self.guard = guard
        self._busy = busy

    def decide(self, src_ip: str, method: str, authorization: str | None) -> GateDecision:
        if self.guard.blocked(src_ip):
            return GateDecision(403, reason="адрес заблокирован после неверных попыток")
        result = self.auth.verify(method, authorization)
        if result is AuthResult.NO_CREDENTIALS:
            return GateDecision(401, self.auth.challenge(), "нужен пароль")
        if result is AuthResult.BAD:
            blocked = self.guard.failure(src_ip)
            return GateDecision(403, reason="неверный логин или пароль" + (", адрес заблокирован на 15 минут" if blocked else ""))
        self.guard.success(src_ip)
        if self._busy():
            return GateDecision(486, reason="линия занята")
        return GateDecision(200, reason="принят")
