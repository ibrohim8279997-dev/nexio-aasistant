"""Управление агентами из Кимо (только владелец): карточка -> подтверждение -> токен в личке -> запуск."""
from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot.agents import builder_flow as flow
from bot.agents.context import AgentContext
from bot.agents.registry import NAME_RE, TOKEN_RE
from bot.config import Settings
from bot.crypto import SecretStore
from bot.handlers.common import show
from bot.keyboards import AgentCb
from bot.logging_setup import register_secret

log = logging.getLogger(__name__)
router = Router(name="agents_admin")

TOKEN_HELP = (
    "🔑 Теперь создайте бота для агента <b>{name}</b>:\n"
    "1. @BotFather → /newbot, задайте имя и username.\n"
    "2. Пришлите токен сюда, в личку. Сообщение с токеном я сразу удалю, токен сохраню зашифрованным.\n"
    "В группе токены не принимаются."
)


class IsOwner(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        return event.from_user is not None and event.from_user.id == settings.admin_id


router.message.filter(IsOwner())
router.callback_query.filter(IsOwner())


def _status(agent: Any) -> str:
    if agent.config.get("draft"):
        return "📝 черновик"
    if agent.config.get("awaiting_token"):
        return "🔑 ждёт токен"
    return "✅ включён" if agent.enabled else "⏸ выключен"


@router.message(Command("agents"))
async def cmd_agents(message: Message, agent_ctx: AgentContext) -> None:
    lines = ["🤖 <b>Агенты</b>"]
    for a in await agent_ctx.registry.all():
        tables = ", ".join(a.allowed_tables) or "нет"
        lines.append(
            f"\n<b>{escape(a.display_name)}</b> (<code>{a.name}</code>, @{escape(a.bot_username or '—')}) — {_status(a)}\n"
            f"{escape(a.role[:200])}\nТаблицы: {escape(tables)} · Whisper: {'да' if a.stt_model else 'нет'}"
            f" · Поиск: {'да' if a.search else 'нет'}"
        )
    lines.append("\n/agentperm имя таблицы · /agenton имя · /agentoff имя · /agenttoken имя токен")
    await message.answer("\n".join(lines))


@router.callback_query(AgentCb.filter())
async def cb_card(call: CallbackQuery, callback_data: AgentCb, bot: Bot, settings: Settings, agent_ctx: AgentContext) -> None:
    name = callback_data.name
    if callback_data.action == "cancel":
        removed = await flow.cancel(agent_ctx, name)
        await call.answer("Отменено" if removed else "Нечего отменять")
        await show(call, f"✖️ Создание агента <code>{escape(name)}</code> отменено." if removed else "Нечего отменять.")
        return
    try:
        row = await flow.approve(agent_ctx, name, with_suggested=callback_data.action == "yes_sugg")
    except flow.FlowError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer("Принято")
    tables = ", ".join(row["allowed_tables"]) or "нет доступа к таблицам"
    commands = ", ".join("/" + c for c in row["allowed_commands"])
    await show(call, f"✅ Агент <code>{name}</code> подтверждён.\nТаблицы: {escape(tables)}\nКоманды: {escape(commands)}")
    try:
        await bot.send_message(settings.admin_id, TOKEN_HELP.format(name=name))
    except TelegramAPIError:
        log.warning("Не удалось написать владельцу в личку")
    if call.message and call.message.chat.type != ChatType.PRIVATE:
        await bot.send_message(call.message.chat.id, "🔑 Токен для нового бота пришлите мне в личку.")


async def _activate(message: Message, agent_ctx: AgentContext, secrets: SecretStore, name: str, token: str) -> None:
    register_secret(token)  # токен не должен попасть в логи этого процесса
    try:
        username = await flow.accept_token(agent_ctx, secrets, name, token)
    except flow.FlowError as exc:
        await message.answer(f"⚠️ {escape(str(exc))}")
        return
    await message.answer(f"🔐 Токен @{escape(username)} сохранён (зашифрован). Запускаю агента <code>{name}</code>…")
    ok, output = await flow.launch(name)
    if not ok:
        await message.answer(f"⚠️ Не удалось запустить: {escape(output)}\nЗапустите вручную: <code>./run.sh start {name}</code>")
        return
    if await flow.wait_alive(agent_ctx, name):
        await message.answer(f"✅ Агент <code>{name}</code> (@{escape(username)}) работает. Добавьте его в группу агентов.")
    else:
        await message.answer(f"⚠️ Агент запущен, но не ответил. Логи: <code>./run.sh logs {name}</code>")


def _has_token(text: str) -> bool:
    return bool(TOKEN_RE.search(text))


@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(_has_token), ~F.text.startswith("/"))
async def on_token(message: Message, agent_ctx: AgentContext, secrets: SecretStore) -> None:
    try:
        await message.delete()  # сообщение с токеном не храним в чате
    except TelegramAPIError:
        pass
    match = TOKEN_RE.search(message.text or "")
    waiting = await flow.awaiting_token(agent_ctx)
    if not waiting:
        await message.answer("Сейчас я не жду токен. Сначала опишите агента и подтвердите карточку.")
        return
    if len(waiting) > 1:
        await message.answer("Токен ждут несколько агентов: " + ", ".join(waiting) + ".\nПришлите: /agenttoken имя токен")
        return
    await _activate(message, agent_ctx, secrets, waiting[0], match.group(0) if match else "")


@router.message(Command("agenttoken"))
async def cmd_agenttoken(message: Message, command: CommandObject, agent_ctx: AgentContext, secrets: SecretStore) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if message.chat.type != ChatType.PRIVATE:
        return  # в группе токены не принимаются (GroupGuard удаляет их раньше)
    parts = (command.args or "").split()
    if len(parts) != 2 or not NAME_RE.match(parts[0]) or not TOKEN_RE.fullmatch(parts[1]):
        await message.answer("Формат: /agenttoken имя токен")
        return
    await _activate(message, agent_ctx, secrets, parts[0], parts[1])


@router.message(Command("agentperm"))
async def cmd_agentperm(message: Message, command: CommandObject, agent_ctx: AgentContext) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not NAME_RE.match(parts[0]):
        await message.answer("Формат: /agentperm имя tasks,rates_cache   (или «-» — без доступа)")
        return
    tables = [] if parts[1] == "-" else [t.strip() for t in parts[1].split(",") if t.strip()]
    try:
        tables = await flow.set_tables(agent_ctx, parts[0], tables)
    except flow.FlowError as exc:
        await message.answer(f"⚠️ {escape(str(exc))}")
        return
    await message.answer(
        f"✅ Таблицы <code>{parts[0]}</code>: {escape(', '.join(tables) or 'нет')}. "
        f"Перезапустите агента: /agentoff {parts[0]}, затем /agenton {parts[0]}"
    )


@router.message(Command("agenton", "agentoff"))
async def cmd_agent_toggle(message: Message, command: CommandObject, agent_ctx: AgentContext) -> None:
    name = (command.args or "").strip()
    agent = await agent_ctx.registry.get(name) if NAME_RE.match(name) else None
    if agent is None or name == agent_ctx.agent.name:
        await message.answer("Укажите агента: /agenton имя  (Кимо так не выключается)")
        return
    if agent.config.get("draft") or agent.config.get("awaiting_token"):
        await message.answer("Агент ещё не готов: подтвердите карточку и пришлите токен.")
        return
    if command.command == "agentoff":
        await agent_ctx.system_db.update_agent(name, enabled=False)
        await flow.stop(name)
        await message.answer(f"⏸ Агент <code>{name}</code> выключен.")
        return
    await agent_ctx.system_db.update_agent(name, enabled=True)
    ok, output = await flow.launch(name)
    alive = ok and await flow.wait_alive(agent_ctx, name)
    await message.answer(
        f"✅ Агент <code>{name}</code> работает." if alive else f"⚠️ Агент не ответил. Логи: <code>./run.sh logs {name}</code>"
    )
