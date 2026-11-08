"""Inline-клавиатуры. Цвет кнопок: поле style (Bot API 9.4+), иначе эмодзи-заглушка."""
from __future__ import annotations

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

STYLE_SUPPORTED = "style" in InlineKeyboardButton.model_fields
_STYLE_EMOJI = {"primary": "🔵", "success": "🟢", "danger": "🔴"}


class Menu(CallbackData, prefix="m"):
    name: str  # main | tasks | exp | stats | sub


class TaskCb(CallbackData, prefix="t"):
    action: str  # list | add | done | move | del | mv1h | mvtom | mvcustom
    task_id: int = 0
    day: str = "today"  # today | tomorrow


class ExpCb(CallbackData, prefix="e"):
    action: str  # add | sum | rate | undo
    period: str = ""


class StatsCb(CallbackData, prefix="s"):
    action: str  # exp_days | exp_cat | tasks_days | tracker


class PayCb(CallbackData, prefix="p"):
    method: str  # stars | click


def btn(
    text: str,
    callback_data: CallbackData | str,
    style: str | None = None,
    icon: str = "",
) -> InlineKeyboardButton:
    """Кнопка с цветом. Если style не поддерживается — цвет заменяется эмодзи вместо иконки."""
    data = callback_data.pack() if isinstance(callback_data, CallbackData) else callback_data
    if style and STYLE_SUPPORTED:
        label = f"{icon} {text}".strip()
        return InlineKeyboardButton(text=label, callback_data=data, style=style)
    if style:
        return InlineKeyboardButton(text=f"{_STYLE_EMOJI[style]} {text}", callback_data=data)
    return InlineKeyboardButton(text=f"{icon} {text}".strip(), callback_data=data)


def _kb(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def home_button() -> InlineKeyboardButton:
    return btn("Главное меню", Menu(name="main"), icon="🏠")


def main_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [btn("Задачи", Menu(name="tasks"), "primary", "📋"), btn("Расходы", Menu(name="exp"), "primary", "💸")],
            [btn("Статистика", Menu(name="stats"), "primary", "📊"), btn("Подписка", Menu(name="sub"), "success", "⭐")],
        ]
    )


def back_home() -> InlineKeyboardMarkup:
    return _kb([[home_button()]])


def cancel_kb() -> InlineKeyboardMarkup:
    return _kb([[btn("Отмена", Menu(name="main"), "danger", "✖️")]])


# ---------- Задачи ----------

def tasks_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn("Сегодня", TaskCb(action="list", day="today"), "primary", "📅"),
                btn("Завтра", TaskCb(action="list", day="tomorrow"), "primary", "🗓"),
            ],
            [btn("Добавить задачу", TaskCb(action="add"), "success", "➕")],
            [home_button()],
        ]
    )


def task_list_kb(items: Sequence[tuple[int, int]], day: str) -> InlineKeyboardMarkup:
    """items — пары (номер строки в списке, id задачи) для невыполненных задач."""
    rows = []
    for num, task_id in items:
        rows.append(
            [
                btn(str(num), TaskCb(action="done", task_id=task_id, day=day), "success", "✅"),
                btn(str(num), TaskCb(action="move", task_id=task_id, day=day), "primary", "⏰"),
                btn(str(num), TaskCb(action="del", task_id=task_id, day=day), "danger", "🗑"),
            ]
        )
    rows.append(
        [
            btn("Добавить", TaskCb(action="add"), "success", "➕"),
            btn("Задачи", Menu(name="tasks"), icon="📋"),
        ]
    )
    return _kb(rows)


def task_move_kb(task_id: int, day: str) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn("+1 час", TaskCb(action="mv1h", task_id=task_id, day=day), "primary", "⏩"),
                btn("На завтра", TaskCb(action="mvtom", task_id=task_id, day=day), "primary", "🗓"),
            ],
            [btn("Своя дата", TaskCb(action="mvcustom", task_id=task_id, day=day), "primary", "✏️")],
            [btn("Отмена", TaskCb(action="list", day=day), "danger", "✖️")],
        ]
    )


# ---------- Расходы ----------

def expenses_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [btn("Добавить расход", ExpCb(action="add"), "success", "➕")],
            [
                btn("День", ExpCb(action="sum", period="day"), "primary", "📅"),
                btn("Неделя", ExpCb(action="sum", period="week"), "primary", "🗓"),
                btn("Месяц", ExpCb(action="sum", period="month"), "primary", "📆"),
            ],
            [
                btn("Курс USD", ExpCb(action="rate"), icon="💱"),
                btn("Удалить последний", ExpCb(action="undo"), "danger", "↩️"),
            ],
            [home_button()],
        ]
    )


# ---------- Статистика ----------

def stats_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn("Расходы по дням", StatsCb(action="exp_days"), "primary", "📈"),
                btn("По категориям", StatsCb(action="exp_cat"), "primary", "🥧"),
            ],
            [
                btn("Трекер задач", StatsCb(action="tracker"), "success", "🏆"),
                btn("Задачи по дням", StatsCb(action="tasks_days"), "primary", "📊"),
            ],
            [home_button()],
        ]
    )


# ---------- Подписка ----------

def subscription_kb(price_xtr: int, click_enabled: bool = False, click_price: int = 0) -> InlineKeyboardMarkup:
    rows = [[btn(f"Оплатить {price_xtr} ⭐", PayCb(method="stars"), "success", "💳")]]
    if click_enabled:
        rows.append([btn(f"Click — {click_price:,} сум".replace(",", " "), PayCb(method="click"), "primary", "💳")])
    rows.append([home_button()])
    return _kb(rows)


def reminder_kb(task_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                btn("Выполнено", TaskCb(action="done", task_id=task_id, day="remind"), "success", "✅"),
                btn("+1 час", TaskCb(action="mv1h", task_id=task_id, day="remind"), "primary", "⏩"),
            ]
        ]
    )


class AgentCb(CallbackData, prefix="ag"):
    action: str  # yes_sugg | yes_none | cancel
    name: str


def agent_card_kb(name: str, has_suggestions: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_suggestions:
        rows.append([btn("Да, с предложенными правами", AgentCb(action="yes_sugg", name=name), "success", "✅")])
    rows.append([btn("Да, без доступа к таблицам", AgentCb(action="yes_none", name=name), "success", "✅")])
    rows.append([btn("Отмена", AgentCb(action="cancel", name=name), "danger", "✖️")])
    return _kb(rows)
