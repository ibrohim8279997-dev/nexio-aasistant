"""Кимо как участник шины: отвечает другим агентам про задачи и напоминания владельца.

Расходы другим агентам не раскрываются: действия с расходами из шины не выполняются.
Голосовой звонок (voice_sip) — отдельный случай: это ввод самого владельца, он идёт тем же путём, что и сообщение
в Telegram (bot/services/pipeline.py), с теми же правами и теми же подтверждениями «да» только в Telegram."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError

from bot.agents.bus import BusError, NeedsClarification, chunks, plain_text
from bot.agents.health import effective_status
from bot.agents.context import AgentContext
from bot.ai.actions import (ActionParseError, AddExpense, Clarify, CreateBroadcast, PauseBusiness, ShowStats, build_messages,
                            parse_actions)
from bot.ai.llm import LLMClient
from bot.config import Settings
from bot.ai.groq_client import GroqError
from bot.services import assistant, grants, pipeline
from bot.services import perms as perms_svc
from bot.services.rates import RatesService
from bot.services.tasks import open_tasks_for_prompt
from bot.logging_setup import redact_text
from bot.timeutil import TZ, context_now_text, from_db, now_local, now_utc, to_db
from bot.voice.policy import check_call

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
    return isinstance(action, (AddExpense, CreateBroadcast, PauseBusiness)) or (
        isinstance(action, ShowStats) and action.topic == "expenses")


VOICE_SERVICE = "voice_sip"
MINIAPP = "miniapp"
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
        self.clock = now_local  # подменяется в тестах (ночное время)
        self.call_wait = 75.0  # сколько ждать итог звонка (30 с на ответ + запас) перед сообщением в Telegram
        self._background: set[asyncio.Task[None]] = set()

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
            ctx=self.ctx, chat_id=self.settings.admin_id, is_group=False, is_owner=True, miniapp_url=self.settings.miniapp_url,
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

    async def _miniapp_user(self, task: dict[str, Any]) -> dict[str, Any]:
        """Пользователь из задания Mini App: должен существовать и иметь активный доступ."""
        payload = task["payload"]
        if task["from_agent"] != MINIAPP or payload.get("source") != "miniapp":
            raise BusError("Это задание принимается только от Mini App")
        try:
            tg_id = int(payload["tg_id"])
        except (KeyError, TypeError, ValueError):
            raise BusError("Нет пользователя") from None
        user = await self.db.get_user_by_tg(tg_id)
        if user is None or not await grants.has_access(self.db, user, self.settings):
            raise BusError("Нет доступа")
        return user

    async def user_command(self, task: dict[str, Any]) -> str:
        """Фраза или типизированное действие из Mini App. Права и подтверждения те же, что в Telegram."""
        payload = task["payload"]
        user = await self._miniapp_user(task)
        is_owner = user["tg_id"] == self.settings.admin_id
        perms = await perms_svc.effective_perms(self.db, user, self.settings.admin_id)
        deps = assistant.AssistantDeps(
            db=self.db, user=user, rates=self.rates, identity=await self.ctx.identity(), ctx=self.ctx,
            chat_id=user["tg_id"], is_group=False, is_owner=is_owner, perms=perms, miniapp_url=self.settings.miniapp_url,
            offer_restart=self.ctx.admin.offer_restart if self.ctx.admin else None, admin=self.ctx.admin,
        )
        if payload.get("action") is not None:  # типизированное действие без обращения к ИИ (управление агентами)
            if not is_owner:
                raise BusError("Только для владельца")
            agents = await self.ctx.registry.aliases(exclude=self.ctx.agent.name)
            try:
                actions = parse_actions(json.dumps({"actions": [payload["action"]]}), now_local().date(), agents)
            except ActionParseError as exc:
                raise BusError("Некорректное действие") from exc
            answers = await assistant.execute(actions, deps)
            for a in answers:  # карточки и подтверждения с кнопками приходят владельцу в Telegram
                if a.markup is not None and self.bot is not None:
                    await self.bot.send_message(user["tg_id"], a.text, reply_markup=a.markup)
            note = "\n\n".join(a.text for a in answers)
            if any(a.markup is not None for a in answers):
                note += "\n\n📨 Подтвердите в Telegram."
            return json.dumps({"text": plain_text(note)}, ensure_ascii=False)
        text = str(payload.get("text", "")).strip()[:1000]
        if not text:
            raise BusError("Пустой запрос")
        from bot.ai.queue import P_CLIENT, P_OWNER

        llm = self.llm.bound(P_OWNER) if is_owner and hasattr(self.llm, "bound") else (
            self.llm.bound(P_CLIENT, owner=user["id"]) if hasattr(self.llm, "bound") else self.llm)
        try:
            answers = await pipeline.interpret(text, llm, deps)
        except GroqError as exc:
            raise BusError(plain_text(exc.user_message)) from exc
        except pipeline.NotUnderstood as exc:
            raise BusError("Не понял запрос. Попробуйте иначе.") from exc
        for a in answers:
            if a.markup is not None and is_owner and self.bot is not None:
                await self.bot.send_message(user["tg_id"], a.text, reply_markup=a.markup)
        return json.dumps({"text": plain_text("\n\n".join(a.text for a in answers))}, ensure_ascii=False)

    async def advice(self, task: dict[str, Any]) -> str:
        """Рекомендации по сводке пользователя: один фоновый запрос раз в сутки (кэш держит Mini App)."""
        await self._miniapp_user(task)
        payload = task["payload"]
        lang = "uz" if payload.get("lang") == "uz" else "ru"
        stats = json.dumps(payload.get("stats") or {}, ensure_ascii=False)[:3000]
        system = (
            "Ты — финансовый и продуктивный помощник. По сводке за 30 дней дай 3–5 коротких практичных советов "
            f"на языке: {'узбекский (латиница)' if lang == 'uz' else 'русский'}. Только по данным сводки, без выдумок. "
            'Ответ — ТОЛЬКО JSON: {"tips":[{"area":"expenses"|"income"|"tasks","text":"<совет до 160 символов>"}]}'
        )
        from bot.ai.queue import P_BACKGROUND

        llm = self.llm.bound(P_BACKGROUND) if hasattr(self.llm, "bound") else self.llm
        try:
            raw = await llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": stats}], max_tokens=600)
        except GroqError as exc:
            raise BusError(plain_text(exc.user_message)) from exc
        try:
            tips = [{"area": str(t.get("area", "tasks")), "text": str(t.get("text", ""))[:200]}
                    for t in json.loads(raw).get("tips", []) if str(t.get("text", "")).strip()][:6]
        except (ValueError, AttributeError):
            raise BusError("ИИ вернул непонятный ответ") from None
        if not tips:
            raise BusError("Нет советов")
        return json.dumps({"tips": tips}, ensure_ascii=False)

    async def _tell_owner(self, text: str) -> None:
        if self.bot is None:
            return
        try:
            for part in chunks(text):
                await self.bot.send_message(self.settings.admin_id, part, parse_mode=None)
        except TelegramAPIError as exc:
            log.warning("Не удалось написать владельцу: %s", type(exc).__name__)

    async def call_owner(self, task: dict[str, Any]) -> str:
        """Любой агент просит позвонить владельцу. Звонит только Кимо: проверка правил, затем place_call службе звонков."""
        payload = task["payload"]
        reason = redact_text(str(payload.get("reason", "")).strip())[:200]
        urgent = bool(payload.get("urgent"))
        if not reason:
            raise BusError("Для звонка нужна короткая причина")
        who = task["from_agent"]
        voice_row = await self.ctx.system_db.get_agent("voice_sip")
        if voice_row is None or not voice_row["enabled"]:
            await self._tell_owner(f"📞 Хотел позвонить ({who}): {reason}\nГолосовая служба не включена.")
            raise BusError("Голосовая служба не включена")
        last = await self.ctx.system_db.audit_list(limit=1, action="voice_call_out")
        last_at = from_db(last[0]["ts"]).astimezone(TZ) if last else None
        decision = check_call(self.clock(), last_at, urgent)
        if not decision.ok:
            await self._tell_owner(f"📞 Хотел позвонить ({who}): {reason}\nНе звоню: {decision.reason}.")
            raise BusError(f"Звонок не выполнен: {decision.reason}. Причина отправлена в Telegram")
        if effective_status(voice_row["last_seen"], voice_row["status"]) == "offline":
            await self._tell_owner(f"📞 Хотел позвонить ({who}): {reason}\nГолосовая служба offline (проверьте ./run.sh status).")
            raise BusError("Голосовая служба offline")
        # попытка засчитывается сразу: два запроса подряд не дадут два звонка
        await self.ctx.system_db.audit(to_db(now_utc()), self.ctx.agent.name, "voice_call_out", None,
                                       {"from": who, "reason": reason, "urgent": urgent})
        call_task = await self.ctx.bus.send(
            "voice_sip", {"type": "place_call", "reason": reason, "urgent": urgent, "requested_by": who},
            depth=int(task["depth"]) + 1, chat_id=self.settings.admin_id,
        )
        job = asyncio.get_running_loop().create_task(self._watch_call(call_task, reason, who))
        self._background.add(job)
        job.add_done_callback(self._background.discard)
        return "Звонок владельцу поставлен в очередь"

    async def _watch_call(self, call_task: int, reason: str, who: str) -> None:
        """Не дозвонились (не ответил, Linphone спит, служба упала) — суть уходит владельцу сообщением в Telegram."""
        try:
            done = await self.ctx.bus.wait_result(call_task, timeout=self.call_wait, interval=0.5)
        except BusError as exc:
            await self._tell_owner(f"📞 Не дозвонился ({who}): {reason}\n{exc}")
            return
        await self.ctx.system_db.queue_mark_delivered(call_task)
        if done["status"] == "failed":
            why = (done["result"] or "причина неизвестна").removeprefix("⚠️").strip()
            await self._tell_owner(f"📞 Не дозвонился ({who}): {reason}\nПричина: {why}")

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
        if kind == "user_command":
            return await self.user_command(task)
        if kind == "advice":
            return await self.advice(task)
        if kind == "tg_message":
            return await self.tg_message(task)
        if kind == "call_owner":
            return await self.call_owner(task)
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
