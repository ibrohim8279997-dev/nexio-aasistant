"""Статистика: графики расходов и задач, трекер."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from bot.db import Database
from bot.handlers.common import show
from bot.keyboards import Menu, StatsCb, stats_menu
from bot.services import charts, tracker
from bot.services import expenses as exp_svc
from bot.services.rates import RatesService
from bot.timeutil import now_local, period_bounds

log = logging.getLogger(__name__)
router = Router(name="stats")

CHART_DAYS = 30
TASK_CHART_DAYS = 14


@router.callback_query(Menu.filter(F.name == "stats"))
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, "📊 <b>Статистика</b>\nВыберите отчёт.", stats_menu())


async def _send_chart(call: CallbackQuery, png: bytes, caption: str) -> None:
    assert call.bot is not None
    await call.bot.send_photo(
        call.from_user.id, BufferedInputFile(png, filename="chart.png"), caption=caption, reply_markup=stats_menu()
    )


@router.callback_query(StatsCb.filter(F.action == "exp_days"))
async def cb_exp_days(call: CallbackQuery, db: Database, rates: RatesService, db_user: dict[str, Any]) -> None:
    today = now_local().date()
    start, end = period_bounds("month", today)
    rows = await db.expenses_between(db_user["id"], start, end)
    if not rows:
        await call.answer("Нет расходов за 30 дней", show_alert=True)
        return
    await call.answer("Строю график…")
    info = await rates.try_get()
    days = [today - timedelta(days=CHART_DAYS - 1 - i) for i in range(CHART_DAYS)]
    values = exp_svc.daily_uzs(rows, info.rate if info else None, days)
    png = await asyncio.to_thread(charts.expenses_by_day, days, values)
    await _send_chart(call, png, "📈 Расходы по дням за 30 дней")


@router.callback_query(StatsCb.filter(F.action == "exp_cat"))
async def cb_exp_cat(call: CallbackQuery, db: Database, rates: RatesService, db_user: dict[str, Any]) -> None:
    start, end = period_bounds("month", now_local().date())
    rows = await db.expenses_between(db_user["id"], start, end)
    info = await rates.try_get()
    totals = exp_svc.compute_totals(rows, info.rate if info else None)
    if not totals.by_category_uzs:
        await call.answer("Нет расходов за 30 дней", show_alert=True)
        return
    await call.answer("Строю график…")
    png = await asyncio.to_thread(
        charts.expenses_by_category, list(totals.by_category_uzs), list(totals.by_category_uzs.values())
    )
    await _send_chart(call, png, "🥧 Расходы по категориям за 30 дней")


@router.callback_query(StatsCb.filter(F.action == "tasks_days"))
async def cb_tasks_days(call: CallbackQuery, db: Database, db_user: dict[str, Any]) -> None:
    data = await tracker.tasks_per_day(db, db_user["id"], TASK_CHART_DAYS)
    if not any(data.total):
        await call.answer("Нет задач за 14 дней", show_alert=True)
        return
    await call.answer("Строю график…")
    png = await asyncio.to_thread(charts.tasks_by_day, data.days, data.done, data.total)
    await _send_chart(call, png, "📊 Выполнение задач за 14 дней")


@router.callback_query(StatsCb.filter(F.action == "tracker"))
async def cb_tracker(call: CallbackQuery, db: Database, db_user: dict[str, Any]) -> None:
    await call.answer()
    await show(call, await tracker.tracker_text(db, db_user["id"]), stats_menu())
