"""Общий агент: assistant, agent_builder и будущие агенты из карточек.

Ответ на запрос — максимум один «инструмент» за запрос:
  1) план (JSON): answer | search | ask_agent;
  2) search -> вызов с browser_search Groq; ask_agent -> задание другому агенту через шину и итоговый ответ.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from typing import Any, Literal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import ErrorEvent, Message
from pydantic import BaseModel, Field, ValidationError

from bot.agents.bus import BusError, BusWorker, DepthExceeded, chunks
from bot.agents.cards import CardError, builder_messages, parse_card
from bot.agents.context import AgentContext
from bot.agents.registry import TOKEN_RE, norm_name
from bot.ai.groq_client import GroqError
from bot.ai.llm import LLMClient
from bot.config import Settings
from bot.middlewares import ThrottlingMiddleware
from bot.timeutil import context_now_text

log = logging.getLogger(__name__)

MAX_TEXT_LEN = 2000
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
_CITATION_RE = re.compile(r"【[^】]*】")


class Plan(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    action: Literal["answer", "search", "ask_agent"]
    text: str | None = Field(default=None, max_length=6000)
    query: str | None = Field(default=None, max_length=500)
    agent: str | None = None
    question: str | None = Field(default=None, max_length=1000)


class PlanError(ValueError):
    pass


def parse_plan(raw: str, allowed_agents: set[str], search_allowed: bool) -> Plan:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise PlanError("нет JSON")
    try:
        plan = Plan.model_validate(json.loads(text[start : end + 1]))
    except (ValueError, ValidationError) as exc:
        raise PlanError(type(exc).__name__) from exc
    if plan.action == "answer" and not plan.text:
        raise PlanError("пустой ответ")
    if plan.action == "search":
        if not search_allowed:
            raise PlanError("поиск выключен")
        if not plan.query:
            raise PlanError("нет запроса для поиска")
    if plan.action == "ask_agent" and (norm_name(plan.agent) not in allowed_agents or not plan.question):
        raise PlanError("неизвестный агент")
    return plan


PLAN_PROMPT = """{identity}

Сейчас: {now}.
Реши, как ответить владельцу. Верни ТОЛЬКО JSON одного из видов:
{{"action": "answer", "text": "готовый ответ"}}
{search_line}{{"action": "ask_agent", "agent": "<имя или системное имя агента>", "question": "что спросить, с контекстом"}} — если нужны данные, которые ведёт другой агент (например, задачи, напоминания, статусы и ошибки агентов — у Кимо).
Отвечай на языке владельца. Не выдумывай факты."""

SEARCH_LINE = '{"action": "search", "query": "поисковый запрос"} — если нужны свежие факты из интернета.\n'


class GenericAgent:
    def __init__(self, ctx: AgentContext, llm: LLMClient, settings: Settings) -> None:
        self.ctx = ctx
        self.llm = llm
        self.settings = settings

    @property
    def name(self) -> str:
        return self.ctx.agent.name

    @property
    def search_allowed(self) -> bool:
        return self.ctx.agent.search and self.settings.search_enabled

    @property
    def max_searches(self) -> int:
        return max(1, min(int(self.ctx.agent.config.get("max_searches", 2)), 5))

    async def answer(self, text: str, depth: int = 0, chat: tuple[int | None, int | None] = (None, None)) -> str:
        identity = await self.ctx.identity()
        others = await self.ctx.registry.aliases(exclude=self.name)
        system = PLAN_PROMPT.format(
            identity=identity, now=context_now_text(), search_line=SEARCH_LINE if self.search_allowed else ""
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        raw = (await self.llm.chat(messages, json_mode=True, agent=self.name)).content
        try:
            plan = parse_plan(raw, others, self.search_allowed)
        except PlanError:
            log.info("План не разобран, отвечаю напрямую")
            return await self._final(identity, text, "")

        if plan.action == "answer":
            return plan.text or ""
        if plan.action == "search":
            return await self._search(identity, text, plan.query or text)
        target = await self.ctx.registry.find(plan.agent or "", enabled_only=True)
        if target is None or target.name == self.name:
            return await self._final(identity, text, "")
        context = {  # кто спрашивает, о чём и что нужно — иначе исполнитель вынужден переспрашивать
            "from": self.name,
            "from_name": self.ctx.agent.display_name,
            "owner_request": text[:500],
            "about": (plan.question or "")[:300],
            "need": f"данные агента {target.display_name}: {(plan.question or '')[:300]}",
        }
        try:
            data = await self.ctx.bus.ask(
                target.name, plan.question or text, depth=depth + 1, chat_id=chat[0], reply_to=chat[1],
                timeout=self.ask_timeout(target), context=context,
            )
        except DepthExceeded as exc:
            return str(exc)
        except BusError as exc:
            return f"⚠️ Не получил данные от {target.display_name}: {exc}"
        return await self._final(identity, text, f"Данные от агента {target.display_name}:\n{data}")

    @staticmethod
    def ask_timeout(target: Any) -> float:
        """Сколько ждать ответ агента: настраивается в его конфиге (task_timeout_s), по умолчанию 60 с."""
        try:
            return float(target.config.get("task_timeout_s", 60)) + 5.0
        except (TypeError, ValueError):
            return 65.0

    async def _search(self, identity: str, text: str, query: str) -> str:
        system = (
            f"{identity}\n\nСейчас: {context_now_text()}.\n"
            f"Найди ответ в интернете. Сделай не больше {self.max_searches} поисков. "
            "Ответь кратко, на языке владельца, в конце укажи 1-3 источника (домены)."
        )
        result = await self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": f"{text}\n\nПоисковый запрос: {query}"}],
            search=True, max_tokens=3000, agent=self.name,
        )
        return _CITATION_RE.sub("", result.content).strip()

    async def _final(self, identity: str, text: str, data: str) -> str:
        system = f"{identity}\n\nСейчас: {context_now_text()}.\nОтветь владельцу кратко и по делу, на его языке."
        if data:
            system += f"\nИспользуй эти данные, ничего не выдумывай:\n{data}"
        result = await self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": text}], agent=self.name
        )
        return result.content.strip()

    async def build_card(self, description: str) -> str:
        identity = await self.ctx.identity()
        existing = [a.name for a in await self.ctx.registry.all()]
        raw = (await self.llm.chat(builder_messages(identity, description, existing), json_mode=True, agent=self.name)).content
        try:
            card = parse_card(raw)
        except CardError as exc:
            raise BusError(f"Не удалось составить карточку: {exc}") from exc
        if card.name in existing:
            card.name = f"{card.name}_2"[:32]
        return card.model_dump_json()

    async def handle_task(self, task: dict[str, Any]) -> str:
        """Обработчик заданий из шины."""
        payload = task.get("payload") or {}
        kind = payload.get("type")
        if kind == "ping":
            return "pong"
        if kind == "build_card":
            if self.ctx.agent.kind != "builder":
                raise BusError("Этот агент не составляет карточки")
            return await self.build_card(str(payload.get("text", ""))[:MAX_TEXT_LEN])
        if kind == "ask":
            return await self.answer(
                str(payload.get("text", ""))[:MAX_TEXT_LEN], depth=int(task["depth"]),
                chat=(task.get("chat_id"), task.get("reply_to_message_id")),
            )
        raise BusError(f"Неизвестный тип задания: {kind}")


def _is_allowed(message: Message, agent: GenericAgent) -> bool:
    user = message.from_user
    if user is None or user.is_bot:
        return False
    return user.id == agent.settings.admin_id or agent.ctx.agent.config.get("audience") == "all"


def _addressed(message: Message, bot_id: int, username: str | None) -> bool:
    """В группе агент отвечает только на упоминание или реплай на своё сообщение."""
    if message.chat.type == ChatType.PRIVATE:
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot_id:
        return True
    text = message.text or message.caption or ""
    return bool(username) and f"@{username}".lower() in text.lower()


def build_router(agent: GenericAgent, bot_id: int, username: str | None) -> Router:
    router = Router(name=f"agent_{agent.name}")

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def on_start(message: Message) -> None:
        if not _is_allowed(message, agent):
            return
        a = agent.ctx.agent
        await message.answer(f"Я {a.display_name}. {a.role}", parse_mode=None)

    async def reply_answer(message: Message, bot: Bot, text: str) -> None:
        try:
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except TelegramAPIError:
            pass
        try:
            answer = await agent.answer(text[:MAX_TEXT_LEN], chat=(message.chat.id, message.message_id))
        except GroqError as exc:
            agent.ctx.state.set_error(f"Groq: {type(exc).__name__}")
            answer = exc.user_message
        for i, part in enumerate(chunks(answer)):
            if i == 0:
                await message.reply(part, parse_mode=None)
            else:
                await message.answer(part, parse_mode=None)

    @router.message(F.voice | F.audio)
    async def on_voice(message: Message, bot: Bot) -> None:
        if not _is_allowed(message, agent) or not _addressed(message, bot_id, username):
            return
        media = message.voice or message.audio
        if media is None:
            return
        if media.file_size and media.file_size > TG_DOWNLOAD_LIMIT:
            await message.answer("Голосовое слишком большое (до 20 МБ).")
            return
        buffer = io.BytesIO()
        try:
            await bot.download(media.file_id, destination=buffer)
            text = await agent.llm.transcribe(buffer.getvalue(), "voice.ogg" if message.voice else "audio.mp3")
        except GroqError as exc:
            await message.answer(exc.user_message)
            return
        except TelegramAPIError:
            await message.answer("⚠️ Не удалось скачать голосовое.")
            return
        if text:
            await message.answer(f"🎙 {text[:MAX_TEXT_LEN]}", parse_mode=None)
            await reply_answer(message, bot, text)

    @router.message(F.text, ~F.text.startswith("/"))
    async def on_text(message: Message, bot: Bot) -> None:
        if not _is_allowed(message, agent) or not _addressed(message, bot_id, username):
            return
        text = message.text or ""
        if TOKEN_RE.search(text):  # токены не отправляем в LLM и не храним в чате
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            await message.answer("🔒 Токены принимает только Кимо — в личке.", parse_mode=None)
            return
        if username:
            text = re.sub(rf"@{re.escape(username)}", "", text, flags=re.IGNORECASE).strip()
        if text:
            await reply_answer(message, bot, text)

    return router


async def run_generic(settings: Settings, ctx: AgentContext, llm: LLMClient, bot: Bot) -> None:
    agent = GenericAgent(ctx, llm, settings)
    me = await bot.get_me()
    if me.username and me.username != ctx.agent.bot_username:
        await ctx.system_db.update_agent(ctx.agent.name, bot_username=me.username)

    dp = Dispatcher()
    throttling = ThrottlingMiddleware(settings.rate_limit_per_min, settings.admin_id)
    dp.message.outer_middleware(throttling)
    dp.include_router(build_router(agent, me.id, me.username))

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Ошибка агента %s: %s", agent.name, type(event.exception).__name__, exc_info=event.exception)
        ctx.state.set_error(f"{type(event.exception).__name__} при обработке сообщения")
        return True

    worker = BusWorker(ctx.bus, agent.handle_task, bot, settings.admin_id, state=ctx.state)
    worker_task = asyncio.create_task(worker.run())
    try:
        log.info("Агент %s (@%s) запущен", agent.name, me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.stop()
        await asyncio.gather(worker_task, return_exceptions=True)
