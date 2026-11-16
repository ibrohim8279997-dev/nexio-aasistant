"""Кимо как участник шины: отвечает другим агентам про задачи и напоминания владельца.

Расходы другим агентам не раскрываются: действия с расходами из шины не выполняются.
Голосовой звонок (voice_sip) — отдельный случай: это ввод самого владельца, он идёт тем же путём, что и сообщение
в Telegram (bot/services/pipeline.py), с теми же правами и теми же подтверждениями «да» только в Telegram."""
from __future__ import annotations

import json
import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError

from bot.agents.bus import BusError, NeedsClarification, chunks, plain_text
from bot.agents.context import AgentContext
from bot.ai.actions import AddExpense, Clarify, ShowStats, build_messages, parse_actions
from bot.ai.llm import LLMClient
from bot.config import Settings
from bot.ai.groq_client import GroqError
from bot.services import assistant, pipeline
from bot.services.rates import RatesService
from bot.services.tasks import open_tasks_for_prompt
from bot.timeutil import context_now_text, now_local, now_utc, to_db

log = logging.getLogger(__name__)

BUS_NOTE = (
    "Запрос пришёл не от владельца напрямую, а от агента {sender} через шину заданий. "
    "Отвечай про задачи и напоминания. Данные о расходах другим агентам не раскрывай."
    "{context}"
)


def context_note(payload: dict[str, Any]) -> str:
    """Контекст запроса из payload: кто спрашивает, о чём, что нужно (см. Bus.prepare_payload)."""
    context = payload.get("context") or {}
    parts = [
        f"{label}: {str(context[key])[:300]}"
        for key, label in (("from_name", "спрашивает"), ("owner_request", "исходный запрос владельца"),
                           ("about", "о чём"), ("need", "что нужно"))
        if context.get(key)
    ]
    return ("\nКонтекст запроса — " + "; ".join(parts) + ".") if parts else ""


def is_private_action(action: Any) -> bool:
    """Действия, которые не выполняются по запросу других агентов."""
    return isinstance(action, AddExpense) or (isinstance(action, ShowStats) and action.topic == "expenses")


VOICE_SERVICE = "voice_sip"
VOICE_NOTE = (
    "Владелец говорит с тобой по телефону: отвечай очень коротко (одно-два предложения), без таблиц, ссылок и логов. "
    "Опасные действия требуют подтверждения «да» ТОЛЬКО текстом в Telegram, голосовое «да» ничего не подтверждает: "
    "скажи, что отправил подтверждение в Telegram."
)


class KimoBusHandler:
    def __init__(self, ctx: AgentContext, db: Any, llm: LLMClient, rates: RatesService, settings: Settings,
                 bot: Any = None) -> None:
        self.ctx = ctx
        self.db = db  # ScopedDB Кимо
        self.llm = llm
        self.rates = rates
        self.settings = settings
        self.bot = bot

    async def voice_command(self, task: dict[str, Any]) -> str:
        """Фраза из звонка: полные права владельца, тот же путь, что у текста из Telegram. Результат — JSON для voice_sip."""
        payload = task["payload"]
        if task["from_agent"] != VOICE_SERVICE or payload.get("source") != "voice_call":
            raise BusError("Голосовые команды принимаются только от службы звонков")
        text = str(payload.get("text", "")).strip()[:1000]
        if not text:
            raise BusError("Пустая фраза")
        owner = await self.db.ensure_user(self.settings.admin_id, to_db(now_utc()))
        history = [(str(h.get("user", "")), str(h.get("kimo", ""))) for h in (payload.get("context") or {}).get("history", [])][-3:]
        deps = assistant.AssistantDeps(
            db=self.db, user=owner, rates=self.rates, identity=await self.ctx.identity() + "\n" + VOICE_NOTE,
            ctx=self.ctx, chat_id=self.settings.admin_id, is_group=False, is_owner=True,
            offer_restart=self.ctx.admin.offer_restart if self.ctx.admin else None, admin=self.ctx.admin,
        )
        try:
            answers = await pipeline.interpret(text, self.llm, deps, history)
        except GroqError as exc:
            self.ctx.state.set_error(f"Groq: {type(exc).__name__}")
            raise BusError(plain_text(exc.user_message)) from exc
        except pipeline.NotUnderstood as exc:
            raise BusError("Кимо не понял фразу. Повторите иначе.") from exc
        answered_by = ["kimo", *dict.fromkeys(deps.consulted)]
        return json.dumps({"text": plain_text("\n\n".join(a.text for a in answers)), "answered_by": answered_by},
                          ensure_ascii=False)

    async def tg_message(self, task: dict[str, Any]) -> str:
        """Текст владельцу в Telegram по просьбе голосовой службы (расшифровка звонка, длинный ответ, поздний результат)."""
        if task["from_agent"] != VOICE_SERVICE:
            raise BusError("Сообщения владельцу принимаются только от службы звонков")
        if self.bot is None:
            raise BusError("У Кимо нет доступа к Telegram")
        text = str(task["payload"].get("text", "")).strip()
        if not text:
            return "пусто"
        try:
            for part in chunks(text[:12000]):
                await self.bot.send_message(self.settings.admin_id, part, parse_mode=None)
        except TelegramAPIError as exc:
            raise BusError(f"Telegram не принял сообщение: {type(exc).__name__}") from exc
        return "отправлено"

    async def __call__(self, task: dict[str, Any]) -> str:
        payload = task.get("payload") or {}
        kind = payload.get("type")
        if kind == "ping":
            return "pong"
        if kind == "voice_command":
            return await self.voice_command(task)
        if kind == "tg_message":
            return await self.tg_message(task)
        if kind != "ask":
            raise BusError(f"Кимо не понимает задание типа {kind}")
        text = str(payload.get("text", ""))[:1000]
        owner = await self.db.ensure_user(self.settings.admin_id, to_db(now_utc()))
        identity = await self.ctx.identity() + "\n" + BUS_NOTE.format(
            sender=task["from_agent"], context=context_note(payload)
        )
        open_tasks = await open_tasks_for_prompt(self.db, owner["id"])
        raw = await self.llm.chat_json(build_messages(text, context_now_text(), open_tasks, identity))
        parsed = parse_actions(raw, now_local().date())
        for action in parsed:
            if isinstance(action, Clarify):
                # не молча done с вопросом внутри: failed с причиной, вопрос уходит владельцу
                raise NeedsClarification(f"Кимо не понял задание, нужно уточнение: {action.question}", action.question)
        actions = [a for a in parsed if not is_private_action(a)]
        if not actions:
            reason = "расходы другим агентам не передаются" if parsed else "не удалось понять запрос"
            raise BusError(f"Кимо не смог выполнить задание: {reason}")
        deps = assistant.AssistantDeps(  # запрос от агента: не владелец, логи и управление недоступны
            db=self.db, user=owner, rates=self.rates, identity=identity, admin=self.ctx.admin, is_owner=False,
        )
        answers = await assistant.execute(actions, deps)
        return plain_text("\n\n".join(a.text for a in answers))
