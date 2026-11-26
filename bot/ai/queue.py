"""Общая очередь запросов к LLM: приоритеты, повтор при 429 с задержкой, лимит на пользователя.

Groq (gpt-oss-120b, бесплатный план): 30 запросов в минуту и 1000 в день, 8000 токенов в минуту на весь ключ.
Очередь стоит перед клиентом внутри процесса агента: сообщения владельца идут раньше ответов клиентам,
а те — раньше фоновых задач (рассылки, AI-советы). Клиент Groq сам считает окна и ждёт retry-after;
очередь добавляет порядок, ограничение параллелизма и отложенный повтор для фоновых запросов.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from bot.ai.groq_client import GroqError, GroqRateLimitError

log = logging.getLogger(__name__)

P_OWNER, P_CLIENT, P_BACKGROUND = 0, 1, 2
RETRY_DELAYS = (5.0, 15.0, 45.0)  # повторы при 429 для клиентских и фоновых запросов


class UserLimitError(GroqError):
    """Пользователь превысил свой лимит обращений к ИИ."""

    def __init__(self, wait: float, daily: bool) -> None:
        super().__init__("user limit")
        self.wait = wait
        self.daily = daily

    @property
    def user_message(self) -> str:  # type: ignore[override]
        if self.daily:
            return "⏳ Дневной лимит обращений к ИИ исчерпан. Попробуйте завтра."
        return f"⏳ Слишком много обращений к ИИ. Подождите {max(int(self.wait), 5)} с."


class UserLimiter:
    """Скользящие окна «в минуту» и «в сутки» на пользователя (owner_id)."""

    def __init__(self, per_min: int, per_day: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_min, self.per_day, self.clock = per_min, per_day, clock
        self._min: dict[int, deque[float]] = defaultdict(deque)
        self._day: dict[int, deque[float]] = defaultdict(deque)

    def check(self, owner_id: int) -> None:
        """Записывает обращение или бросает UserLimitError."""
        now = self.clock()
        minute, day = self._min[owner_id], self._day[owner_id]
        while minute and now - minute[0] >= 60:
            minute.popleft()
        while day and now - day[0] >= 86400:
            day.popleft()
        if self.per_day > 0 and len(day) >= self.per_day:
            raise UserLimitError(86400 - (now - day[0]), daily=True)
        if self.per_min > 0 and len(minute) >= self.per_min:
            raise UserLimitError(60 - (now - minute[0]), daily=False)
        minute.append(now)
        day.append(now)


class LLMQueue:
    def __init__(self, concurrency: int = 2, limiter: UserLimiter | None = None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.concurrency = max(1, concurrency)
        self.limiter = limiter
        self.retry_delays = retry_delays
        self._heap: list[tuple[int, int, Callable[[], Awaitable[Any]], asyncio.Future[Any]]] = []
        self._seq = itertools.count()
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._closed = False

    def _ensure_workers(self) -> None:
        if not self._workers:
            self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def submit(self, prio: int, factory: Callable[[], Awaitable[Any]], owner: int | None = None) -> Any:
        if self._closed:
            raise GroqError("очередь остановлена")
        if owner is not None and self.limiter is not None:
            self.limiter.check(owner)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (prio, next(self._seq), factory, fut))
        self._ensure_workers()
        self._wake.set()
        return await fut

    async def _worker(self) -> None:
        while not self._closed:
            if not self._heap:
                self._wake.clear()
                await self._wake.wait()
                continue
            prio, _, factory, fut = heapq.heappop(self._heap)
            if fut.cancelled():
                continue
            try:
                result = await self._run(prio, factory)
            except BaseException as exc:  # noqa: BLE001 — исключение уходит вызывающему
                if not fut.cancelled():
                    fut.set_exception(exc)
            else:
                if not fut.cancelled():
                    fut.set_result(result)

    async def _run(self, prio: int, factory: Callable[[], Awaitable[Any]]) -> Any:
        delays = iter(self.retry_delays if prio > P_OWNER else ())
        while True:
            try:
                return await factory()
            except GroqRateLimitError as exc:
                delay = next(delays, None)
                if delay is None or exc.daily:
                    raise
                wait = max(delay, exc.retry_after or 0.0)
                log.warning("LLM-очередь: 429, повтор через %.0f с (приоритет %s)", wait, prio)
                await asyncio.sleep(wait)

    def pending(self) -> int:
        return len(self._heap)

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        for _, _, _, fut in self._heap:
            if not fut.done():
                fut.set_exception(GroqError("очередь остановлена"))
        self._heap.clear()


class QueuedLLM:
    """Обёртка над клиентом LLM: все вызовы идут через очередь. bound() — тот же клиент с другим приоритетом/владельцем."""

    def __init__(self, inner: Any, queue: LLMQueue, prio: int = P_OWNER, owner: int | None = None) -> None:
        self._inner, self._queue, self.prio, self.owner = inner, queue, prio, owner

    def bound(self, prio: int, owner: int | None = None) -> QueuedLLM:
        return QueuedLLM(self._inner, self._queue, prio, owner)

    def __getattr__(self, name: str) -> Any:  # text_model, stt_model, last_limits, rate_limit_until ...
        return getattr(self._inner, name)

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        return await self._queue.submit(self.prio, lambda: self._inner.chat(messages, **kwargs), self.owner)

    async def chat_json(self, messages: list[dict[str, str]], max_tokens: int = 2048) -> str:
        return await self._queue.submit(self.prio, lambda: self._inner.chat_json(messages, max_tokens), self.owner)

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        return await self._queue.submit(self.prio, lambda: self._inner.transcribe(audio, filename), self.owner)

    async def close(self) -> None:
        await self._queue.close()
        await self._inner.close()
