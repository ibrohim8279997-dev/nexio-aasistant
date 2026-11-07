"""Команды администратора (только ADMIN_ID)."""
from __future__ import annotations

import re
from html import escape
from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message

from bot.config import Settings
from bot.crypto import SecretStore
from bot.db import Database
from bot.services import subscription as sub
from bot.timeutil import now_utc, to_db

router = Router(name="admin")

MAX_GRANT_DAYS = 3650
SECRET_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")

ADMIN_HELP = (
    "<b>Админ-команды</b>\n"
    "/grant &lt;user_id&gt; &lt;дней&gt; — выдать/продлить доступ\n"
    "/revoke &lt;user_id&gt; — отключить доступ\n"
    "/users [страница] — пользователи\n"
    "/stats — статистика бота\n"
    "/setsecret &lt;имя&gt; &lt;значение&gt; — сохранить секрет (шифруется)\n"
    "/secrets — список секретов, /delsecret &lt;имя&gt; — удалить"
)


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return message.from_user is not None and message.from_user.id == settings.admin_id


router.message.filter(IsAdmin())


def _args(command: CommandObject) -> list[str]:
    return (command.args or "").split()


@router.message(Command("adminhelp"))
async def cmd_help(message: Message) -> None:
    await message.answer(ADMIN_HELP)


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    args = _args(command)
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer("Формат: /grant &lt;user_id&gt; &lt;дней&gt;")
        return
    tg_id, days = int(args[0]), int(args[1])
    if not 1 <= days <= MAX_GRANT_DAYS:
        await message.answer(f"Дней: от 1 до {MAX_GRANT_DAYS}.")
        return
    user = await db.ensure_user(tg_id, to_db(now_utc()))
    until = await sub.grant(db, user, days)
    await message.answer(f"✅ Доступ для {tg_id} до {sub.fmt_until(to_db(until))}")
    try:
        await bot.send_message(tg_id, f"🎁 Вам открыт доступ к ассистенту до {sub.fmt_until(to_db(until))}. /menu")
    except TelegramAPIError:
        await message.answer("(Пользователь ещё не запускал бота — уведомление не отправлено.)")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, db: Database) -> None:
    args = _args(command)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Формат: /revoke &lt;user_id&gt;")
        return
    user = await db.get_user_by_tg(int(args[0]))
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    await sub.revoke(db, user)
    await message.answer(f"⛔ Доступ для {user['tg_id']} отключён.")


@router.message(Command("users"))
async def cmd_users(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    args = _args(command)
    page = int(args[0]) if args and args[0].isdigit() and int(args[0]) > 0 else 1
    per_page = 30
    users = await db.list_users(limit=per_page, offset=(page - 1) * per_page)
    if not users:
        await message.answer("Пользователей нет.")
        return
    now = now_utc()
    lines = [f"<b>Пользователи</b> (стр. {page}, всего {await db.count_users()})"]
    for u in users:
        name = f"@{u['username']}" if u["username"] else (u["first_name"] or "")
        if u["tg_id"] == settings.admin_id:
            status = "админ"
        elif sub.is_active(u["sub_until"], now):
            status = f"до {sub.fmt_until(u['sub_until'])}"
        else:
            status = "нет подписки"
        lines.append(f"<code>{u['tg_id']}</code> {escape(name)} — {status}")
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    now = to_db(now_utc())
    pay = await db.payments_summary()
    await message.answer(
        "<b>Статистика бота</b>\n"
        f"Пользователей: {await db.count_users()}\n"
        f"Активных подписок: {await db.count_active_subs(now)}\n"
        f"Платежей: {pay['n']} (⭐ {pay['xtr']}, сум {pay['uzs']})\n"
        f"Задач: {await db.count_tasks()}\n"
        f"Расходов: {await db.count_expenses()}"
    )


@router.message(Command("setsecret"))
async def cmd_setsecret(message: Message, command: CommandObject, secrets: SecretStore) -> None:
    try:
        await message.delete()  # сообщение содержит секрет — убираем из чата
    except TelegramAPIError:
        pass
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not SECRET_NAME_RE.match(parts[0]):
        await message.answer("Формат: /setsecret &lt;имя a-z0-9_&gt; &lt;значение&gt;")
        return
    await secrets.set(parts[0], parts[1].strip())
    await message.answer(f"🔐 Секрет <code>{parts[0]}</code> сохранён (зашифрован). Сообщение с ним удалено.")


@router.message(Command("secrets"))
async def cmd_secrets(message: Message, secrets: SecretStore) -> None:
    names = await secrets.names()
    await message.answer("🔐 Секреты: " + (", ".join(f"<code>{n}</code>" for n in names) if names else "нет"))


@router.message(Command("delsecret"))
async def cmd_delsecret(message: Message, command: CommandObject, secrets: SecretStore) -> None:
    name = (command.args or "").strip()
    if not SECRET_NAME_RE.match(name):
        await message.answer("Формат: /delsecret &lt;имя&gt;")
        return
    deleted = await secrets.delete(name)
    await message.answer("Удалено." if deleted else "Нет такого секрета.")

