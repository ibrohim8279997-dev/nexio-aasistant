"""/start, /help, главное меню."""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.db import Database
from bot.handlers.common import show
from bot.timeutil import now_utc, to_db
from bot.keyboards import Menu, main_menu

router = Router(name="start")

HELP_TEXT = (
    "<b>Что я умею</b>\n\n"
    "📋 <b>Задачи</b> — пишите или говорите: «завтра в 15:00 позвонить маме». Напомню вовремя.\n"
    "💸 <b>Расходы</b> — «такси 25000» или «обед 12 долларов». Сумы и доллары, курс ЦБ.\n"
    "📊 <b>Статистика</b> — итоги, графики, трекер выполнения задач.\n\n"
    "🎙 Можно отправлять голосовые на русском и узбекском.\n\n"
    "Команды:\n"
    "/menu — главное меню\n"
    "/add <i>текст</i> — задача без ИИ: «/add завтра 09:30 спортзал»\n"
    "/spend <i>сумма [usd] категория [заметка]</i> — расход без ИИ\n"
    "/today, /tomorrow — задачи\n"
    "/subscription — подписка, /paysupport — помощь с оплатой\n"
    "/cancel — отменить ввод"
)


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^sb_(\d+)$")), flags={"public": True})
async def cmd_start_subscribe(message: Message, command: CommandObject, db: Database, bot: Bot, state: FSMContext) -> None:
    """Ссылка владельца t.me/<бот>?start=sb_<id>: человек сам подписывается на сообщения от Кимо."""
    await state.clear()
    owner = await db.get_user_by_tg(int(command.args[3:]))
    if owner is None or message.from_user is None or message.from_user.is_bot or message.chat.type != "private":
        await message.answer("Ссылка недействительна.")
        return
    await db.bsub_add(owner["id"], message.chat.id, message.from_user.full_name, to_db(now_utc()))
    who = owner.get("first_name") or "владельца"
    await message.answer(f"Готово! Я Кимо, AI-агент {escape(who)}. Теперь я могу писать вам сообщения от имени бота. "
                         "Чтобы отписаться, напишите «стоп».")
    try:
        await bot.send_message(owner["tg_id"], f"🔔 Новый подписчик на рассылки: {message.from_user.full_name} (ID {message.chat.id}). "
                               f"Создать рассылку: /bcast add {message.chat.id} 10:00 daily ai спроси, как дела", parse_mode=None)
    except TelegramAPIError:
        pass


@router.message(CommandStart(), flags={"public": True})
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    name = message.from_user.first_name if message.from_user else ""
    await message.answer(
        f"Привет, {escape(name)}! Я ваш личный ассистент.\n\n{HELP_TEXT}",
        reply_markup=main_menu(),
    )


@router.message(Command("help"), flags={"public": True})
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(Command("menu", "cancel"), flags={"public": True})
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())


@router.callback_query(Menu.filter(F.name == "main"), flags={"public": True})
async def cb_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, "Главное меню:", main_menu())

