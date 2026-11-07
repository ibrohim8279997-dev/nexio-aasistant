"""/start, /help, главное меню."""
from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.common import show
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
    "/subscription — подписка\n"
    "/cancel — отменить ввод"
)


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


@router.message(Command("menu", "cancel"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())


@router.callback_query(Menu.filter(F.name == "main"))
async def cb_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, "Главное меню:", main_menu())

