"""Подтверждения «да» / «нет» от владельца: кнопки и текст в личке. Подходит и Кимо, и Мимо (у каждого свой actor)."""
from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from bot.agents.confirm import NO_WORDS, YES_WORDS, ConfirmError, Confirmations
from bot.keyboards import ConfirmCb

router = Router(name="confirm")


class AnswersPending(BaseFilter):
    """Сообщение «да»/«нет» в личке владельца, пока у этого агента есть ожидающие подтверждения."""

    async def __call__(self, message: Message, confirm: Confirmations) -> bool:
        if message.chat.type != ChatType.PRIVATE or not message.from_user or message.from_user.id != confirm.owner_id:
            return False
        word = (message.text or "").strip().casefold().rstrip(".!")
        return word in YES_WORDS | NO_WORDS and bool(await confirm.open_pending())


@router.callback_query(ConfirmCb.filter())
async def cb_confirm(call: CallbackQuery, callback_data: ConfirmCb, confirm: Confirmations) -> None:
    private = bool(call.message and call.message.chat.type == ChatType.PRIVATE)
    try:
        text = await confirm.decide(callback_data.id, bool(callback_data.yes), call.from_user.id, private)
    except ConfirmError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer("Готово")
    if call.message and hasattr(call.message, "edit_reply_markup"):
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
    if call.bot is not None:
        await call.bot.send_message(call.from_user.id, text, parse_mode=None)


@router.message(AnswersPending())
async def text_confirm(message: Message, confirm: Confirmations) -> None:
    pending = await confirm.open_pending()
    if len(pending) != 1:
        await message.answer(f"Ожидают подтверждения {len(pending)} действий. Нажмите «Да» или «Нет» под нужным сообщением.")
        return
    word = (message.text or "").strip().casefold().rstrip(".!")
    try:
        text = await confirm.decide(pending[0]["id"], word in YES_WORDS, message.from_user.id, True)
    except ConfirmError as exc:
        await message.answer(str(exc), parse_mode=None)
        return
    await message.answer(text, parse_mode=None)
