"""Общие помощники хендлеров."""
from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def send(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Новое сообщение пользователю, нажавшему кнопку."""
    assert call.bot is not None
    await call.bot.send_message(call.from_user.id, text, reply_markup=markup)


async def show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Обновить сообщение с кнопками, а если нельзя (фото, старое) — отправить новое."""
    message = call.message
    if isinstance(message, Message) and message.text is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return
    await send(call, text, markup)
