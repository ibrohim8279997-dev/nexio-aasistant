"""Регистрация роутеров. Порядок важен: свободный текст (ИИ) и fallback — последними."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot.handlers import admin, expenses, payments, start, stats, tasks, voice

fallback_router = Router(name="fallback")


@fallback_router.callback_query()
async def stale_callback(call: CallbackQuery) -> None:
    await call.answer("Кнопка устарела. Откройте /menu", show_alert=False)


@fallback_router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда. Список команд: /help")


@fallback_router.message()
async def unsupported(message: Message) -> None:
    await message.answer("Я понимаю текст и голосовые сообщения. Меню: /menu")


def get_routers() -> list[Router]:
    return [admin.router, start.router, payments.router, tasks.router, expenses.router, stats.router, voice.router, fallback_router]
