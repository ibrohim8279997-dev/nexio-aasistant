"""Расходы: добавление, итоги, курс, отмена последнего."""
from __future__ import annotations

from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.db import Database
from bot.handlers.common import send, show
from bot.keyboards import ExpCb, Menu, cancel_kb, expenses_menu
from bot.services import expenses as svc
from bot.services.rates import RatesService, RateUnavailableError

router = Router(name="expenses")

ADD_HINT = (
    "Напишите расход: сумма, категория и заметка.\n"
    "• «такси 25000»\n"
    "• «еда 45 000 обед с коллегами»\n"
    "• «кофе 3 usd» или «$12 подписка»\n\n"
    "Или отправьте голосовое."
)


class ExpenseStates(StatesGroup):
    adding = State()


@router.callback_query(Menu.filter(F.name == "exp"))
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, "💸 <b>Расходы</b>\nДобавьте расход или посмотрите итоги.", expenses_menu())


async def _add_from_text(message: Message, raw: str, db: Database, rates: RatesService, user_id: int) -> bool:
    try:
        p = svc.parse_expense_input(raw)
        _, info = await svc.create_expense(db, rates, user_id, p.amount, p.currency, p.category, p.note)
    except svc.ExpenseInputError as exc:
        await message.answer(f"⚠️ {exc}\n\n{ADD_HINT}", reply_markup=cancel_kb())
        return False
    await message.answer(svc.created_text(p.amount, p.currency, p.category, p.note, info), reply_markup=expenses_menu())
    return True


@router.message(Command("spend"))
async def cmd_spend(
    message: Message, command: CommandObject, state: FSMContext, db: Database, rates: RatesService, db_user: dict[str, Any]
) -> None:
    if command.args:
        await _add_from_text(message, command.args, db, rates, db_user["id"])
        return
    await state.set_state(ExpenseStates.adding)
    await message.answer(ADD_HINT, reply_markup=cancel_kb())


@router.callback_query(ExpCb.filter(F.action == "add"))
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ExpenseStates.adding)
    await call.answer()
    await send(call, ADD_HINT, cancel_kb())


@router.message(ExpenseStates.adding, F.text, ~F.text.startswith("/"))
async def on_add_text(message: Message, state: FSMContext, db: Database, rates: RatesService, db_user: dict[str, Any]) -> None:
    if await _add_from_text(message, message.text or "", db, rates, db_user["id"]):
        await state.clear()


@router.callback_query(ExpCb.filter(F.action == "sum"))
async def cb_summary(call: CallbackQuery, callback_data: ExpCb, db: Database, rates: RatesService, db_user: dict[str, Any]) -> None:
    period = callback_data.period if callback_data.period in svc.PERIOD_TITLES else "day"
    await call.answer()
    await show(call, await svc.summary_text(db, rates, db_user["id"], period), expenses_menu())


@router.callback_query(ExpCb.filter(F.action == "rate"))
async def cb_rate(call: CallbackQuery, rates: RatesService) -> None:
    try:
        info = await rates.get_usd_rate()
    except RateUnavailableError:
        await call.answer("Курс сейчас недоступен (cbu.uz не отвечает). Попробуйте позже.", show_alert=True)
        return
    await call.answer()
    await show(call, svc.rate_text(info), expenses_menu())


@router.callback_query(ExpCb.filter(F.action == "undo"))
async def cb_undo(call: CallbackQuery, db: Database, db_user: dict[str, Any]) -> None:
    last = await db.last_expense(db_user["id"])
    if last is None or not await db.delete_expense(db_user["id"], last["id"]):
        await call.answer("Расходов нет", show_alert=True)
        return
    await call.answer("Удалено")
    await show(
        call,
        f"↩️ Удалён расход: {svc.fmt_money(last['amount'], last['currency'])} — {escape(last['category'])}",
        expenses_menu(),
    )
