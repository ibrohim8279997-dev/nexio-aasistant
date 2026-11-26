"""Проверка initData Telegram Mini App (HMAC-SHA256 по токену бота) и лимиты запросов."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SECONDS, now: float | None = None) -> dict[str, Any]:
    """Возвращает объект user из initData или бросает AuthError. Токен нигде не логируется и не возвращается."""
    if not init_data or len(init_data) > 4096 or not bot_token:
        raise AuthError("нет initData")
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise AuthError("initData не разобран") from None
    received = data.pop("hash", "")
    if not received:
        raise AuthError("нет hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise AuthError("подпись не совпала")
    try:
        auth_date = int(data["auth_date"])
    except (KeyError, ValueError):
        raise AuthError("нет auth_date") from None
    current = time.time() if now is None else now
    if current - auth_date > max_age or auth_date - current > 300:
        raise AuthError("initData устарел")
    try:
        user = json.loads(data["user"])
        if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:
            raise ValueError
    except (KeyError, ValueError):
        raise AuthError("нет user") from None
    if user.get("is_bot"):
        raise AuthError("бот")
    return user


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Собрать подписанную строку initData (для тестов и проверки)."""
    from urllib.parse import urlencode

    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


class RateLimiter:
    """Скользящее окно: не больше limit событий за window секунд на ключ."""

    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[Any, deque[float]] = defaultdict(deque)

    def allow(self, key: Any) -> bool:
        now = self.clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10000:  # защита от раздувания памяти
            for k in [k for k, v in self._hits.items() if not v]:
                del self._hits[k]
        return True
