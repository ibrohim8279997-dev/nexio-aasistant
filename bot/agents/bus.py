"""Шина заданий между агентами через общую таблицу tasks_queue.

Боты в Telegram не видят сообщения друг друга, поэтому агенты общаются через SQLite:
- send/ask — поставить задание другому агенту;
- claim — атомарно забрать своё задание (UPDATE ... WHERE status='new');
- depth растёт на 1 при каждой передаче; при depth > MAX_DEPTH задание сразу failed (защита от зацикливания).

Доставка ответа (колонки reply и delivered, payload.deliver_by):
- sender  — результат читает и отправляет в чат тот, кто создал задание (по умолчанию для ask);
- inline  — создатель ждёт результат в этом же процессе; если не дождался, результат всё равно будет доставлен позже;
- receiver — ответ в чат сразу отправляет исполнитель (передача задачи в группе).
Каждый результат доставляется один раз: delivered ставится атомарно.
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
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import ReplyParameters

from bot.agents.health import AgentState
from bot.agents.registry import NAME_RE
from bot.db import Database
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

MAX_DEPTH = 3
POLL_INTERVAL = 1.5
ASK_TIMEOUT = 120.0
LATE_GRACE = 5.0  # после дедлайна inline-ожидания результат доставляет фоновый цикл
DEPTH_MESSAGE = (
    "⛔ Задача остановлена: её передавали между агентами больше {max} раз (возможно зацикливание). "
    "Уточните, кто должен её выполнить."
)
TG_LIMIT = 4000
ASK_TYPES = {"ask"}


class BusError(Exception):
    """Задание не выполнено (failed, таймаут, неизвестный агент)."""


class DepthExceeded(BusError):
    def __init__(self, task_id: int) -> None:
        super().__init__(DEPTH_MESSAGE.format(max=MAX_DEPTH))
        self.task_id = task_id


class NeedsClarification(BusError):
    """Исполнитель не понял задание: задание получает failed с причиной, а вопрос уходит владельцу."""

    def __init__(self, reason: str, question: str) -> None:
        super().__init__(reason)
        self.question = question


def plain_text(html_text: str) -> str:
    """Ответ с HTML-разметкой -> обычный текст (для передачи другому агенту)."""
    return unescape(re.sub(r"<[^>]+>", "", html_text or "")).strip()


def chunks(text: str, size: int = TG_LIMIT) -> list[str]:
    text = text or "—"
    return [text[i : i + size] for i in range(0, len(text), size)]


class Bus:
    def __init__(self, db: Database, agent: str, owner_id: int | None = None, display: str | None = None) -> None:
        self._db = db
        self.agent = agent
        self.owner_id = owner_id  # чат владельца: запасной chat_id для заданий без чата
        self.display = display or agent

    def prepare_payload(self, payload: dict[str, Any], chat_id: int | None, reply_to: int | None
                        ) -> tuple[dict[str, Any], int | None, int | None]:
        """Правила шины: контекст у ask, reply по умолчанию, чат владельца вместо пустого chat_id."""
        payload = dict(payload)
        kind = payload.get("type", "ask")
        payload.setdefault("type", kind)
        payload.setdefault("reply", kind in ASK_TYPES)  # ответ нужен вызывающему — true для ask
        if payload["reply"]:
            payload.setdefault("deliver_by", "sender")
        if kind in ASK_TYPES:
            context = dict(payload.get("context") or {})
            context.setdefault("from", self.agent)
            context.setdefault("from_name", self.display)
            if not context.get("about"):
                context["about"] = str(payload.get("text", ""))[:300]
                context["auto"] = True  # контекст дополнен шиной, а не создателем задания
            payload["context"] = context
        if chat_id is None and self.owner_id:
            chat_id, reply_to = self.owner_id, None
            payload["chat_fallback"] = True  # явная пометка: чат не был указан, взят чат владельца
        return payload, chat_id, reply_to

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
        payload, chat_id, reply_to = self.prepare_payload(payload, chat_id, reply_to)
        now = to_db(now_utc())
        if depth > MAX_DEPTH:
            task_id = await self._db.queue_insert(
                self.agent, to_agent, payload, now, chat_id=chat_id, reply_to_message_id=reply_to,
                depth=depth, status="failed", result=DEPTH_MESSAGE.format(max=MAX_DEPTH),
                reply=bool(payload["reply"]), delivered=True,  # создатель узнаёт об остановке из исключения
            )
            log.warning("Задание #%s остановлено: depth=%s", task_id, depth)
            raise DepthExceeded(task_id)
        task_id = await self._db.queue_insert(
            self.agent, to_agent, payload, now, chat_id=chat_id, reply_to_message_id=reply_to, depth=depth,
            reply=bool(payload["reply"]),
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

    async def finish(self, task_id: int, result: str, ok: bool = True) -> bool:
        return await self._db.queue_finish(task_id, "done" if ok else "failed", result, to_db(now_utc()))

    async def wait_result(self, task_id: int, timeout: float = ASK_TIMEOUT, interval: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            task = await self._db.queue_get(task_id)
            if task and task["status"] in ("done", "failed"):
                return task
            if time.monotonic() >= deadline:
                raise BusError("Агент не ответил вовремя (возможно, он не запущен)")
            await asyncio.sleep(interval)

    async def ask(
        self,
        to_agent: str,
        text: str,
        *,
        depth: int = 1,
        timeout: float = ASK_TIMEOUT,
        chat_id: int | None = None,
        reply_to: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Спросить другого агента и дождаться ответа. Контекст (кто и зачем спрашивает) обязателен для ask."""
        payload: dict[str, Any] = {"type": "ask", "text": text}
        if context:
            payload["context"] = context
        return await self.request(to_agent, payload, depth=depth, timeout=timeout, chat_id=chat_id, reply_to=reply_to)

    async def request(
        self,
        to_agent: str,
        payload: dict[str, Any],
        *,
        depth: int = 1,
        timeout: float = ASK_TIMEOUT,
        chat_id: int | None = None,
        reply_to: int | None = None,
    ) -> str:
        """Задание с ожиданием результата в этом процессе: ask, ping, build_card ...

        Если результат получен здесь, он помечается доставленным. Если ждать не дождались, результат придёт позже
        через цикл доставки (после дедлайна) и не потеряется."""
        payload = {**payload, "wait_deadline": time.time() + timeout}
        payload.setdefault("deliver_by", "inline")
        task_id = await self.send(to_agent, payload, depth=depth, chat_id=chat_id, reply_to=reply_to)
        task = await self.wait_result(task_id, timeout)
        await self._db.queue_mark_delivered(task_id)
        if task["status"] == "failed":
            raise BusError(task["result"] or "Задание не выполнено")
        return task["result"] or ""


TaskHandler = Callable[[dict[str, Any]], Awaitable[str]]


class BusWorker:
    """Фоновый цикл агента: доставить свои готовые результаты, забрать задание, выполнить, записать result."""

    def __init__(
        self,
        bus: Bus,
        handler: TaskHandler,
        bot: Bot,
        owner_id: int,
        interval: float = POLL_INTERVAL,
        state: AgentState | None = None,
    ) -> None:
        self.bus = bus
        self.handler = handler
        self.bot = bot
        self.owner_id = owner_id
        self.interval = interval
        self.state = state or AgentState()
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        log.info("Шина: агент %s слушает очередь (каждые %.1f с)", self.bus.agent, self.interval)
        while not self._stopped.is_set():
            try:
                await self.deliver_results()
                task = await self.bus.claim()
            except Exception:  # noqa: BLE001 — БД занята и т.п., пробуем снова
                log.exception("Шина: ошибка чтения очереди")
                self.state.set_error("ошибка чтения очереди")
                task = None
            if task is None:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
                continue
            await self.process(task)

    async def deliver_results(self) -> int:
        """Результаты заданий, созданных этим агентом: отправить в чат реплаем и отметить delivered = 1."""
        sent = 0
        db = self.bus._db
        for row in await db.queue_undelivered(self.bus.agent):
            payload = row["payload"]
            mode = payload.get("deliver_by", "sender")
            if mode == "receiver":
                continue  # ответит исполнитель
            if mode == "inline" and time.time() < float(payload.get("wait_deadline", 0)) + LATE_GRACE:
                continue  # результат ещё ждёт тот, кто создал задание в этом процессе
            if not await db.queue_mark_delivered(row["id"]):
                continue  # уже доставлен другим циклом
            text = row["result"] or "—"
            if row["status"] == "failed" and not text.startswith("⚠️"):
                text = f"⚠️ {text}"
            if mode == "inline":
                text = f"⏱ Ответ пришёл с опозданием:\n{text}"
            if not await self.reply(row["chat_id"], row["reply_to_message_id"], text):
                await db.queue_unmark_delivered(row["id"])  # сеть недоступна — повторим
                break
            sent += 1
        return sent

    async def process(self, task: dict[str, Any]) -> None:
        ok, stopped, question = True, False, None
        self.state.busy_tasks += 1
        try:
            result = await self.handler(task)
        except DepthExceeded as exc:
            ok, stopped, result = False, True, str(exc)
        except NeedsClarification as exc:
            ok, question, result = False, exc.question, f"⚠️ {exc}"
        except BusError as exc:
            ok, result = False, f"⚠️ {exc}"
        except Exception as exc:  # noqa: BLE001
            ok = False
            user_message = getattr(exc, "user_message", None)
            result = str(user_message) if user_message else "⚠️ Агент не смог выполнить задание."
            self.state.set_error(f"{type(exc).__name__}: {user_message or exc}")
            if not user_message:
                log.exception("Шина: задание #%s упало", task["id"])
        finally:
            self.state.busy_tasks = max(self.state.busy_tasks - 1, 0)
        if not await self.bus.finish(task["id"], result, ok):
            log.warning("Шина: задание #%s уже закрыто (таймаут?), результат отброшен", task["id"])
            return
        payload = task.get("payload") or {}
        db = self.bus._db
        if payload.get("deliver_by") == "receiver" and task.get("chat_id") and payload.get("reply", True):
            if await db.queue_mark_delivered(task["id"]):
                await self.reply(task["chat_id"], task.get("reply_to_message_id"), result)
        if question:  # вопрос уточнения уходит владельцу, а не молча внутри result
            await self.notify_owner(f"❓ {question}")
        elif stopped:  # остановка по depth — владелец должен узнать
            await self.notify_owner(f"Задание #{task['id']} ({task['from_agent']} → {self.bus.agent}): {result}")

    async def reply(self, chat_id: int | None, reply_to: int | None, text: str) -> bool:
        """Ответ в чат реплаем от имени этого агента. False — не удалось из-за сети (стоит повторить)."""
        if not chat_id:
            return True
        for i, part in enumerate(chunks(text)):
            try:
                params = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to and i == 0 else None
                await self.bot.send_message(chat_id, part, parse_mode=None, reply_parameters=params)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 5))
                return False
            except TelegramNetworkError as exc:
                log.warning("Шина: нет связи с Telegram: %s", type(exc).__name__)
                return False
            except TelegramAPIError as exc:
                log.warning("Шина: не удалось ответить в чат: %s", type(exc).__name__)
                return True  # повтор не поможет (например, бот заблокирован)
        return True

    async def notify_owner(self, text: str) -> None:
        try:
            await self.bot.send_message(self.owner_id, text, parse_mode=None)
        except TelegramAPIError as exc:
            log.warning("Не удалось уведомить владельца: %s", type(exc).__name__)
