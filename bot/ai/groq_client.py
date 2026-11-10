"""Клиент Groq: текстовая модель (JSON-действия) и Whisper (распознавание речи).

Один ключ GROQ_API_KEY на обе модели. Имена моделей — только из .env.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)
usage_log = logging.getLogger("usage")  # отдельный файл logs/usage-<агент>.log, без текстов и секретов

BASE_URL = "https://api.groq.com/openai/v1"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # лимит Groq на файл
MAX_RETRIES = 3
MAX_RETRY_WAIT = 20.0  # дольше не ждём внутри запроса пользователя

# Лимиты бесплатного тарифа Groq (с запасом не превышаем их сами)
TEXT_LIMITS = (30, 1000)  # запросов в минуту, в день
STT_LIMITS = (20, 2000)
TOKEN_LIMITS = (8_000, 200_000)  # токенов в минуту, в день (gpt-oss-120b, free)
LOW_TOKENS_LEFT = 800  # если по заголовкам Groq осталось меньше — ждём сброса окна


class GroqError(Exception):
    """Базовая ошибка Groq. user_message — что показать пользователю."""

    user_message = "⚠️ ИИ сейчас недоступен. Попробуйте позже."


class GroqRateLimitError(GroqError):
    def __init__(self, retry_after: float | None = None, daily: bool = False) -> None:
        super().__init__("rate limit")
        self.retry_after = retry_after
        self.daily = daily

    @property
    def user_message(self) -> str:  # type: ignore[override]
        if self.daily:
            return "⏳ Дневной лимит запросов к ИИ исчерпан. Попробуйте завтра или используйте кнопки меню."
        wait = int(self.retry_after or 60)
        return f"⏳ Слишком много запросов к ИИ. Попробуйте через {max(wait, 5)} с."


class GroqUnavailableError(GroqError):
    user_message = "⚠️ Нет связи с ИИ (Groq). Попробуйте позже или используйте кнопки меню."


class GroqBadResponseError(GroqError):
    user_message = "🤔 Не удалось разобрать ответ ИИ. Попробуйте сформулировать иначе."

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SttDisabledError(GroqError):
    user_message = "🎙 Этот агент не принимает голосовые. Напишите текстом."


class AudioTooLargeError(GroqError):
    user_message = "Файл слишком большой (максимум 25 МБ)."


class RateWindow:
    """Локальный счётчик запросов: скользящие окна минута/сутки."""

    def __init__(self, per_minute: int, per_day: int) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._minute: deque[float] = deque()
        self._day: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._minute and now - self._minute[0] >= 60:
            self._minute.popleft()
        while self._day and now - self._day[0] >= 86400:
            self._day.popleft()

    def wait_time(self, now: float | None = None) -> tuple[float, bool]:
        """(секунд ждать, это дневной лимит?). 0 — можно отправлять."""
        now = time.monotonic() if now is None else now
        self._trim(now)
        if len(self._day) >= self.per_day:
            return 86400 - (now - self._day[0]), True
        if len(self._minute) >= self.per_minute:
            return 60 - (now - self._minute[0]), False
        return 0.0, False

    def hit(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._minute.append(now)
        self._day.append(now)


class TokenWindow:
    """Счётчик потраченных токенов: скользящие окна минута/сутки (по usage из ответов)."""

    def __init__(self, per_minute: int, per_day: int) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._events: deque[tuple[float, int]] = deque()

    def add(self, tokens: int, now: float | None = None) -> None:
        self._events.append((time.monotonic() if now is None else now, max(tokens, 0)))

    def wait_time(self, now: float | None = None) -> tuple[float, bool]:
        now = time.monotonic() if now is None else now
        while self._events and now - self._events[0][0] >= 86400:
            self._events.popleft()
        day = sum(t for _, t in self._events)
        if day >= self.per_day:
            return 86400 - (now - self._events[0][0]), True
        minute = [(ts, t) for ts, t in self._events if now - ts < 60]
        if sum(t for _, t in minute) >= self.per_minute:
            return 60 - (now - minute[0][0]), False
        return 0.0, False


_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def parse_reset(value: str | None) -> float | None:
    """Заголовок x-ratelimit-reset-* («7.66s», «2m59.5s», «120ms») -> секунды."""
    if not value:
        return None
    m = _DURATION_RE.match(value.strip())
    if not m or not any(m.groups()):
        return None
    h, mnt, sec, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mnt * 60 + sec + ms / 1000


@dataclass
class ChatResult:
    content: str
    usage: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, str] = field(default_factory=dict)
    searches: int = 0


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class GroqClient:
    def __init__(self, api_key: str, text_model: str, stt_model: str, client: httpx.AsyncClient | None = None) -> None:
        self.text_model = text_model
        self.stt_model = stt_model
        self._http = client or httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._text_window = RateWindow(*TEXT_LIMITS)
        self._stt_window = RateWindow(*STT_LIMITS)
        self._tokens = TokenWindow(*TOKEN_LIMITS)
        self.last_limits: dict[str, str] = {}
        self.rate_limit_until = 0.0  # unix-время, до которого действует 429 (heartbeat пишет busy/rate_limit)
        self.on_rate_limit: Callable[[float, bool], Awaitable[None]] | None = None  # уведомить владельца
        self._notified_at = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def _notify_rate_limit(self, wait: float, exhausted: bool) -> None:
        """Сообщить владельцу о лимите Groq (не чаще раза в 5 минут), ошибки уведомления не мешают запросу."""
        if self.on_rate_limit is None or time.time() - self._notified_at < 300:
            return
        self._notified_at = time.time()
        try:
            await self.on_rate_limit(wait, exhausted)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось уведомить о лимите Groq")

    async def _reserve_tokens(self) -> None:
        """Перед текстовым запросом: свой учёт токенов и остаток по заголовкам Groq (общий на все процессы)."""
        wait, daily = self._tokens.wait_time()
        if daily:
            raise GroqRateLimitError(wait, daily=True)
        remaining = self.last_limits.get("x-ratelimit-remaining-tokens")
        if not wait and remaining and remaining.isdigit() and int(remaining) < LOW_TOKENS_LEFT:
            wait = parse_reset(self.last_limits.get("x-ratelimit-reset-tokens")) or 0.0
            self.last_limits.pop("x-ratelimit-remaining-tokens", None)
        if wait > 0:
            if wait > MAX_RETRY_WAIT:
                raise GroqRateLimitError(wait)
            log.info("Groq: жду %.1f с — лимит токенов в минуту", wait)
            await asyncio.sleep(wait)

    async def _reserve(self, window: RateWindow) -> None:
        wait, daily = window.wait_time()
        if daily:
            raise GroqRateLimitError(wait, daily=True)
        if wait > 0:
            if wait > 5:
                raise GroqRateLimitError(wait)
            await asyncio.sleep(wait)
        window.hit()

    async def _post(self, url: str, window: RateWindow, **kwargs: Any) -> dict[str, Any]:
        await self._reserve(window)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self._http.post(url, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
                log.warning("Groq: сетевая ошибка %s (попытка %s)", type(exc).__name__, attempt)
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if response.status_code == 429:
                wait = _retry_after(response) or float(2**attempt)
                log.warning("Groq 429 на %s, retry-after=%.1f (попытка %s)", url, wait, attempt)
                self.rate_limit_until = time.time() + wait
                await self._notify_rate_limit(wait, attempt == MAX_RETRIES or wait > MAX_RETRY_WAIT)
                if wait > MAX_RETRY_WAIT or attempt == MAX_RETRIES:
                    raise GroqRateLimitError(wait, daily=wait > 3600)
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 500:
                log.warning("Groq %s на %s (попытка %s)", response.status_code, url, attempt)
                last_exc = GroqUnavailableError(f"HTTP {response.status_code}")
                await asyncio.sleep(min(2**attempt, 8))
                continue
            if response.status_code >= 400:
                log.error("Groq %s на %s: %s", response.status_code, url, _error_text(response))
                if response.status_code in (401, 403):
                    raise GroqUnavailableError("auth")
                raise GroqBadResponseError(f"HTTP {response.status_code}", status=response.status_code)
            self.rate_limit_until = 0.0
            self.last_limits = {k: v for k, v in response.headers.items() if k.startswith("x-ratelimit-")}
            try:
                return response.json()
            except ValueError as exc:
                raise GroqBadResponseError("не JSON") from exc
        raise GroqUnavailableError(type(last_exc).__name__ if last_exc else "unknown")

    async def chat_json(self, messages: list[dict[str, str]], max_tokens: int = 2048) -> str:
        """Возвращает текст ответа модели (ожидается JSON-объект)."""
        return (await self.chat(messages, json_mode=True, max_tokens=max_tokens)).content

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        search: bool = False,
        max_tokens: int = 2048,
        agent: str = "",
    ) -> ChatResult:
        """Текстовый запрос. search=True — встроенный browser_search Groq (только gpt-oss)."""
        if not self.text_model:
            raise GroqUnavailableError("нет текстовой модели")
        payload: dict[str, Any] = {
            "model": self.text_model,
            "messages": messages,
            "temperature": 0.2,
            "max_completion_tokens": max_tokens,
        }
        if json_mode and not search:  # browser_search несовместим со structured outputs
            payload["response_format"] = {"type": "json_object"}
        if "gpt-oss" in self.text_model:
            payload["reasoning_effort"] = "low"  # меньше токенов и короче поиск
        if search:
            payload["tools"] = [{"type": "browser_search"}]
            payload["tool_choice"] = "required"

        await self._reserve_tokens()
        try:
            data = await self._post("/chat/completions", self._text_window, json=payload)
        except GroqBadResponseError as exc:
            if exc.status != 400 or "response_format" not in payload:
                raise
            # Модель из конфига может не поддерживать JSON-режим — повторяем без него, JSON всё равно проверим
            payload.pop("response_format", None)
            payload.pop("reasoning_effort", None)
            data = await self._post("/chat/completions", self._text_window, json=payload)
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise GroqBadResponseError("нет choices") from exc
        usage = data.get("usage") or {}
        self._tokens.add(int(usage.get("total_tokens") or 0))
        searches = len(message.get("executed_tools") or []) if isinstance(message, dict) else 0
        self._log_usage(agent, search, usage, searches)
        if not content:
            raise GroqBadResponseError("пустой ответ")
        return ChatResult(str(content), usage, dict(self.last_limits), searches)

    def _log_usage(self, agent: str, search: bool, usage: dict[str, Any], searches: int) -> None:
        usage_log.info(
            "agent=%s model=%s search=%s searches=%s prompt=%s completion=%s total=%s "
            "remaining_tokens=%s remaining_requests=%s reset_tokens=%s",
            agent or "-", self.text_model, search, searches,
            usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens"),
            self.last_limits.get("x-ratelimit-remaining-tokens"),
            self.last_limits.get("x-ratelimit-remaining-requests"),
            self.last_limits.get("x-ratelimit-reset-tokens"),
        )

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """Речь -> текст. Язык определяется автоматически (русский, узбекский и др.)."""
        if not self.stt_model:
            raise SttDisabledError("нет модели STT")
        if len(audio) > MAX_AUDIO_BYTES:
            raise AudioTooLargeError("too large")
        data = await self._post(
            "/audio/transcriptions",
            self._stt_window,
            files={"file": (filename, audio, "application/octet-stream")},
            data={"model": self.stt_model, "response_format": "json", "temperature": "0"},
        )
        text = data.get("text") if isinstance(data, dict) else None
        if text is None:
            raise GroqBadResponseError("нет text")
        return str(text).strip()


def _error_text(response: httpx.Response) -> str:
    try:
        err = response.json().get("error", {})
        return f"{err.get('code') or err.get('type')}: {str(err.get('message', ''))[:200]}"
    except Exception:  # noqa: BLE001
        return response.text[:200]
