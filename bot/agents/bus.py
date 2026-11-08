"""Шина заданий между агентами через общую таблицу tasks_queue.

Боты в Telegram не видят сообщения друг друга, поэтому агенты общаются через SQLite:
- send/ask — поставить задание другому агенту;
- claim — атомарно забрать своё задание (UPDATE ... WHERE status='new');
- depth растёт на 1 при каждой передаче; при depth > MAX_DEPTH задание сразу failed (защита от зацикливания).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from html import unescape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.agents.registry import NAME_RE
from bot.db import Database
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

MAX_DEPTH = 3
POLL_INTERVAL = 1.5
ASK_TIMEOUT = 120.0
DEPTH_MESSAGE = (
    "⛔ Задача остановлена: её передавали между агентами больше {max} раз (возможно зацикливание). "
    "Уточните, кто должен её выполнить."
)
TG_LIMIT = 4000


class BusError(Exception):
    """Задание не выполнено (failed, таймаут, неизвестный агент)."""


class DepthExceeded(BusError):
    def __init__(self, task_id: int) -> None:
        super().__init__(DEPTH_MESSAGE.format(max=MAX_DEPTH))
        self.task_id = task_id


def plain_text(html_text: str) -> str:
    """Ответ с HTML-разметкой -> обычный текст (для передачи другому агенту)."""
    return unescape(re.sub(r"<[^>]+>", "", html_text or "")).strip()


def chunks(text: str, size: int = TG_LIMIT) -> list[str]:
    text = text or "—"
    return [text[i : i + size] for i in range(0, len(text), size)]


class Bus:
    def __init__(self, db: Database, agent: str) -> None:
        self._db = db
        self.agent = agent

    async def send(
        self,
        to_agent: str,
        payload: dict[str, Any],
        *,
        depth: int = 1,
        chat_id: int | None = None,
        reply_to: int | None = None,
    ) -> int:
        """Поставить задание. depth — номер передачи (первая передача = 1)."""
        if not NAME_RE.match(to_agent) or to_agent == self.agent:
            raise BusError(f"Некорректный получатель: {to_agent}")
        now = to_db(now_utc())
        if depth > MAX_DEPTH:
            task_id = await self._db.queue_insert(
                self.agent, to_agent, payload, now, chat_id=chat_id, reply_to_message_id=reply_to,
                depth=depth, status="failed", result=DEPTH_MESSAGE.format(max=MAX_DEPTH),
            )
            log.warning("Задание #%s остановлено: depth=%s", task_id, depth)
            raise DepthExceeded(task_id)
        task_id = await self._db.queue_insert(
            self.agent, to_agent, payload, now, chat_id=chat_id, reply_to_message_id=reply_to, depth=depth
        )
        log.info("Задание #%s: %s -> %s (depth=%s)", task_id, self.agent, to_agent, depth)
        return task_id

    async def forward(self, task: dict[str, Any], to_agent: str, payload: dict[str, Any] | None = None) -> int:
        """Передать полученное задание дальше (depth + 1, тот же чат и реплай)."""
        return await self.send(
            to_agent, payload or task["payload"], depth=int(task["depth"]) + 1,
            chat_id=task["chat_id"], reply_to=task["reply_to_message_id"],
        )

    async def claim(self) -> dict[str, Any] | None:
        return await self._db.queue_claim(self.agent, to_db(now_utc()))

    async def finish(self, task_id: int, result: str, ok: bool = True) -> None:
        await self._db.queue_finish(task_id, "done" if ok else "failed", result, to_db(now_utc()))

    async def wait_result(self, task_id: int, timeout: float = ASK_TIMEOUT, interval: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            task = await self._db.queue_get(task_id)
            if task and task["status"] in ("done", "failed"):
                return task
            if time.monotonic() >= deadline:
                raise BusError("Агент не ответил вовремя (возможно, он не запущен)")
            await asyncio.sleep(interval)

    async def ask(self, to_agent: str, text: str, *, depth: int = 1, timeout: float = ASK_TIMEOUT) -> str:
        """Спросить другого агента и дождаться ответа (ответ в чат не отправляется)."""
        task_id = await self.send(to_agent, {"type": "ask", "text": text, "reply": False}, depth=depth)
        task = await self.wait_result(task_id, timeout)
        if task["status"] == "failed":
            raise BusError(task["result"] or "Задание не выполнено")
        return task["result"] or ""


TaskHandler = Callable[[dict[str, Any]], Awaitable[str]]


class BusWorker:
    """Фоновый опрос очереди: забрать задание, выполнить, записать результат, ответить в чат."""

    def __init__(
        self,
        bus: Bus,
        handler: TaskHandler,
        bot: Bot,
        owner_id: int,
        interval: float = POLL_INTERVAL,
    ) -> None:
        self.bus = bus
        self.handler = handler
        self.bot = bot
        self.owner_id = owner_id
        self.interval = interval
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        log.info("Шина: агент %s слушает очередь (каждые %.1f с)", self.bus.agent, self.interval)
        while not self._stopped.is_set():
            try:
                task = await self.bus.claim()
            except Exception:  # noqa: BLE001 — БД занята и т.п., пробуем снова
                log.exception("Шина: ошибка чтения очереди")
                task = None
            if task is None:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
                continue
            await self.process(task)

    async def process(self, task: dict[str, Any]) -> None:
        ok, stopped = True, False
        try:
            result = await self.handler(task)
        except DepthExceeded as exc:
            ok, stopped, result = False, True, str(exc)
        except BusError as exc:
            ok, result = False, f"⚠️ {exc}"
        except Exception as exc:  # noqa: BLE001
            ok = False
            user_message = getattr(exc, "user_message", None)
            result = str(user_message) if user_message else "⚠️ Агент не смог выполнить задание."
            if not user_message:
                log.exception("Шина: задание #%s упало", task["id"])
        await self.bus.finish(task["id"], result, ok)
        payload = task.get("payload") or {}
        if task.get("chat_id") and payload.get("reply", True):
            await self.reply(task["chat_id"], task.get("reply_to_message_id"), result)
        elif stopped:  # остановка по depth — владелец должен узнать, даже если ответа в чат не ждали
            await self.notify_owner(f"Задание #{task['id']} ({task['from_agent']} → {self.bus.agent}): {result}")

    async def reply(self, chat_id: int, reply_to: int | None, text: str) -> None:
        for i, part in enumerate(chunks(text)):
            try:
                await self.bot.send_message(
                    chat_id, part, parse_mode=None,
                    reply_to_message_id=reply_to if i == 0 else None,
                    allow_sending_without_reply=True,
                )
            except TelegramAPIError as exc:
                log.warning("Шина: не удалось ответить в чат: %s", type(exc).__name__)
                return

    async def notify_owner(self, text: str) -> None:
        try:
            await self.bot.send_message(self.owner_id, text, parse_mode=None)
        except TelegramAPIError as exc:
            log.warning("Не удалось уведомить владельца: %s", type(exc).__name__)
