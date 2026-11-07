"""Задачи: списки, добавление, выполнено, перенос, удаление."""
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
from bot.keyboards import Menu, TaskCb, cancel_kb, task_list_kb, task_move_kb, tasks_menu
from bot.services import tasks as svc
from bot.timeutil import from_db, now_local, to_db

router = Router(name="tasks")

ADD_HINT = (
    "Напишите задачу. Можно с датой и временем:\n"
    "• «завтра 15:00 позвонить маме»\n"
    "• «12.11 09:30 врач»\n"
    "• «купить хлеб» — на сегодня\n\n"
    "Или отправьте голосовое."
)
MOVE_HINT = "Введите новую дату и/или время: «завтра 18:00», «12.11 09:30», «15:00»."


class TaskStates(StatesGroup):
    adding = State()
    moving = State()


# ---------- открыть раздел и списки ----------

@router.callback_query(Menu.filter(F.name == "tasks"))
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, "📋 <b>Задачи</b>\nВыберите список или добавьте новую задачу.", tasks_menu())


async def _render(db: Database, user_id: int, day: str) -> tuple[str, Any]:
    view = await svc.render_day(db, user_id, day)
    return view.text, task_list_kb(view.buttons, day)


@router.message(Command("today", "tomorrow"))
async def cmd_list(message: Message, command: CommandObject, db: Database, db_user: dict[str, Any]) -> None:
    text, kb = await _render(db, db_user["id"], command.command)
    await message.answer(text, reply_markup=kb)


@router.callback_query(TaskCb.filter(F.action == "list"))
async def cb_list(call: CallbackQuery, callback_data: TaskCb, state: FSMContext, db: Database, db_user: dict[str, Any]) -> None:
    await state.clear()
    await call.answer()
    day = callback_data.day if callback_data.day in ("today", "tomorrow") else "today"
    text, kb = await _render(db, db_user["id"], day)
    await show(call, text, kb)


# ---------- добавление ----------

async def _add_from_text(message: Message, raw: str, db: Database, user_id: int) -> bool:
    now = now_local()
    try:
        parsed = svc.parse_task_input(raw, now.date())
        due, has_time = svc.resolve_due(parsed.day, parsed.at, now)
        text = svc.clean_task_text(parsed.text)
        await svc.create_task(db, user_id, text, due, has_time)
    except svc.TaskInputError as exc:
        await message.answer(f"⚠️ {exc}\n\n{ADD_HINT}", reply_markup=cancel_kb())
        return False
    await message.answer(svc.created_text(text, due, has_time), reply_markup=tasks_menu())
    return True


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, state: FSMContext, db: Database, db_user: dict[str, Any]) -> None:
    if command.args:
        await _add_from_text(message, command.args, db, db_user["id"])
        return
    await state.set_state(TaskStates.adding)
    await message.answer(ADD_HINT, reply_markup=cancel_kb())


@router.callback_query(TaskCb.filter(F.action == "add"))
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TaskStates.adding)
    await call.answer()
    await send(call, ADD_HINT, cancel_kb())


@router.message(TaskStates.adding, F.text, ~F.text.startswith("/"))
async def on_add_text(message: Message, state: FSMContext, db: Database, db_user: dict[str, Any]) -> None:
    if await _add_from_text(message, message.text or "", db, db_user["id"]):
        await state.clear()


# ---------- действия с задачей ----------

async def _get_task(call: CallbackQuery, db: Database, user_id: int, task_id: int) -> dict[str, Any] | None:
    task = await db.get_task(user_id, task_id)
    if task is None:
        await call.answer("Задача не найдена", show_alert=True)
    return task


async def _after_change(call: CallbackQuery, db: Database, user_id: int, day: str, toast: str, remind_text: str) -> None:
    await call.answer(toast)
    if day == "remind":
        await show(call, remind_text)
        return
    text, kb = await _render(db, user_id, day)
    await show(call, text, kb)


@router.callback_query(TaskCb.filter(F.action == "done"))
async def cb_done(call: CallbackQuery, callback_data: TaskCb, db: Database, db_user: dict[str, Any]) -> None:
    task = await _get_task(call, db, db_user["id"], callback_data.task_id)
    if task is None:
        return
    await db.complete_task(db_user["id"], task["id"], to_db(now_local()))
    await _after_change(
        call, db, db_user["id"], callback_data.day, "✅ Выполнено", f"✅ Выполнено: <s>{escape(task['text'])}</s>"
    )


@router.callback_query(TaskCb.filter(F.action == "del"))
async def cb_delete(call: CallbackQuery, callback_data: TaskCb, db: Database, db_user: dict[str, Any]) -> None:
    task = await _get_task(call, db, db_user["id"], callback_data.task_id)
    if task is None:
        return
    await db.delete_task(db_user["id"], task["id"])
    await _after_change(call, db, db_user["id"], callback_data.day, "🗑 Удалено", "🗑 Задача удалена")


@router.callback_query(TaskCb.filter(F.action == "move"))
async def cb_move(call: CallbackQuery, callback_data: TaskCb, db: Database, db_user: dict[str, Any]) -> None:
    task = await _get_task(call, db, db_user["id"], callback_data.task_id)
    if task is None:
        return
    await call.answer()
    await show(call, f"⏰ Перенести: <b>{escape(task['text'])}</b>", task_move_kb(task["id"], callback_data.day))


@router.callback_query(TaskCb.filter(F.action.in_({"mv1h", "mvtom"})))
async def cb_move_quick(call: CallbackQuery, callback_data: TaskCb, db: Database, db_user: dict[str, Any]) -> None:
    task = await _get_task(call, db, db_user["id"], callback_data.task_id)
    if task is None:
        return
    now = now_local()
    due = from_db(task["due_at"]) or now
    if callback_data.action == "mv1h":
        new_due, has_time = svc.move_one_hour(due, bool(task["has_time"]), now), True
    else:
        new_due, has_time = svc.move_to_tomorrow(due, bool(task["has_time"]), now), bool(task["has_time"])
    await db.reschedule_task(db_user["id"], task["id"], to_db(new_due), has_time)
    when = svc.human_due(new_due, has_time)
    await _after_change(
        call, db, db_user["id"], callback_data.day, f"Перенесено: {when}",
        f"⏰ <b>{escape(task['text'])}</b>\nПеренесено: {when}",
    )


@router.callback_query(TaskCb.filter(F.action == "mvcustom"))
async def cb_move_custom(call: CallbackQuery, callback_data: TaskCb, state: FSMContext, db: Database, db_user: dict[str, Any]) -> None:
    task = await _get_task(call, db, db_user["id"], callback_data.task_id)
    if task is None:
        return
    await state.set_state(TaskStates.moving)
    await state.update_data(task_id=task["id"], day=callback_data.day)
    await call.answer()
    await send(call, MOVE_HINT, cancel_kb())


@router.message(TaskStates.moving, F.text, ~F.text.startswith("/"))
async def on_move_text(message: Message, state: FSMContext, db: Database, db_user: dict[str, Any]) -> None:
    data = await state.get_data()
    now = now_local()
    try:
        parsed = svc.parse_task_input(message.text or "", now.date())
        if parsed.day is None and parsed.at is None:
            raise svc.TaskInputError("Не нашёл дату или время.")
        due, has_time = svc.resolve_due(parsed.day, parsed.at, now)
        svc.check_due_range(due, now)
    except svc.TaskInputError as exc:
        await message.answer(f"⚠️ {exc}\n{MOVE_HINT}", reply_markup=cancel_kb())
        return
    ok = await db.reschedule_task(db_user["id"], int(data.get("task_id", 0)), to_db(due), has_time)
    await state.clear()
    if not ok:
        await message.answer("Задача не найдена.", reply_markup=tasks_menu())
        return
    day = "tomorrow" if due.date() == svc.day_for("tomorrow", now) else "today"
    text, kb = await _render(db, db_user["id"], day)
    await message.answer(f"⏰ Перенесено: {svc.human_due(due, has_time)}")
    await message.answer(text, reply_markup=kb)
