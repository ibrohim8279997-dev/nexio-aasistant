"""Регистрация роутеров. Порядок важен: свободный текст (ИИ) — последним."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from bot.handlers import start

fallback_router = Router(name="fallback")


@fallback_router.callback_query()
async def stale_callback(call: CallbackQuery) -> None:
    await call.answer("Кнопка устарела. Откройте /menu", show_alert=False)


def get_routers() -> list[Router]:
    return [start.router, fallback_router]
