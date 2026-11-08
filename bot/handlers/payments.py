"""Оплата подписки: Telegram Stars (XTR). Click через Telegram Payments — заготовка, выключена."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from bot.config import Settings
from bot.crypto import DecryptError, SecretStore
from bot.db import Database
from bot.handlers.common import show
from bot.keyboards import Menu, PayCb, back_home, main_menu, subscription_kb
from bot.services import subscription as sub
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)
router = Router(name="payments")

PUBLIC = {"public": True}


def _sub_kb(settings: Settings):
    if not settings.subscription_enabled:
        return back_home()
    return subscription_kb(settings.sub_price_xtr, settings.click_enabled, settings.click_price_uzs)


async def _sales_closed(call: CallbackQuery, settings: Settings) -> bool:
    if settings.subscription_enabled:
        return False
    await call.answer("Подписка сейчас не продаётся.", show_alert=True)
    return True


@router.message(Command("subscription"), flags=PUBLIC)
async def cmd_subscription(message: Message, settings: Settings, db_user: dict[str, Any]) -> None:
    await message.answer(sub.status_text(db_user, settings), reply_markup=_sub_kb(settings))


@router.callback_query(Menu.filter(F.name == "sub"), flags=PUBLIC)
async def cb_subscription(call: CallbackQuery, settings: Settings, db_user: dict[str, Any]) -> None:
    await call.answer()
    await show(call, sub.status_text(db_user, settings), _sub_kb(settings))


@router.message(Command("paysupport"), flags=PUBLIC)
async def cmd_paysupport(message: Message) -> None:
    await message.answer(
        "💬 <b>Поддержка по оплате</b>\n"
        "Опишите проблему одним сообщением и укажите дату оплаты — администратор ответит."
    )


@router.callback_query(PayCb.filter(F.method == "stars"), flags=PUBLIC)
async def cb_pay_stars(call: CallbackQuery, bot: Bot, settings: Settings) -> None:
    if await _sales_closed(call, settings):
        return
    payload = sub.Payload("stars", call.from_user.id, settings.sub_days)
    try:
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=f"Подписка на {settings.sub_days} дней",
            description="Полный доступ к ассистенту: задачи, расходы, голосовые, графики.",
            payload=payload.pack(),
            currency="XTR",
            prices=[LabeledPrice(label=f"Подписка {settings.sub_days} дней", amount=settings.sub_price_xtr)],
            provider_token="",
        )
    except TelegramAPIError as exc:
        log.error("Не удалось выставить счёт Stars: %s", type(exc).__name__)
        await call.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)
        return
    await call.answer()


@router.callback_query(PayCb.filter(F.method == "click"), flags=PUBLIC)
async def cb_pay_click(call: CallbackQuery, bot: Bot, settings: Settings, secrets: SecretStore) -> None:
    # TODO(Click): заготовка. Перед включением (CLICK_ENABLED=true) получите токен провайдера Click
    # в @BotFather -> Payments, проверьте оплату в тестовом режиме и условия Click для подписок.
    if await _sales_closed(call, settings):
        return
    if not settings.click_enabled:
        await call.answer("Этот способ оплаты недоступен.", show_alert=True)
        return
    try:
        token = settings.click_provider_token or await secrets.get("click_provider_token")
    except DecryptError:
        token = None
    if not token:
        log.error("Click включён, но токен провайдера не задан")
        await call.answer("Оплата через Click временно недоступна.", show_alert=True)
        return
    payload = sub.Payload("click", call.from_user.id, settings.sub_days)
    try:
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=f"Подписка на {settings.sub_days} дней",
            description="Полный доступ к ассистенту. Оплата через Click.",
            payload=payload.pack(),
            currency="UZS",
            prices=[LabeledPrice(label=f"Подписка {settings.sub_days} дней", amount=settings.click_price_uzs * 100)],
            provider_token=token,
        )
    except TelegramAPIError as exc:
        log.error("Не удалось выставить счёт Click: %s", type(exc).__name__)
        await call.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)
        return
    await call.answer()


@router.pre_checkout_query(flags=PUBLIC)
async def on_pre_checkout(query: PreCheckoutQuery, settings: Settings) -> None:
    payload = sub.validate_payment(
        query.invoice_payload, query.currency, query.total_amount, query.from_user.id, settings
    )
    try:
        if payload is None:
            log.warning("Отклонён pre_checkout: неверные параметры счёта")
            await query.answer(ok=False, error_message="Счёт устарел. Откройте /subscription и оплатите заново.")
        else:
            await query.answer(ok=True)
    except TelegramAPIError as exc:
        log.error("Не удалось ответить на pre_checkout: %s", type(exc).__name__)


@router.message(F.successful_payment, flags=PUBLIC)
async def on_successful_payment(
    message: Message, bot: Bot, db: Database, settings: Settings, db_user: dict[str, Any]
) -> None:
    payment = message.successful_payment
    assert payment is not None
    payload = sub.Payload.parse(payment.invoice_payload)
    if payload is None or payload.tg_id != db_user["tg_id"]:
        log.error("Платёж с неожиданными параметрами, charge_id сохранён для проверки")
    is_stars = payment.currency == "XTR"
    is_new = await db.add_payment(
        db_user["id"],
        payment.telegram_payment_charge_id,
        payment.total_amount if is_stars else 0,
        to_db(now_utc()),
        provider="stars" if is_stars else "click",
        amount_uzs=None if is_stars else payment.total_amount // 100,
    )
    if not is_new:
        return  # повторный апдейт — подписку уже продлили

    days = payload.days if payload else settings.sub_days
    until = await sub.grant(db, db_user, days)
    await message.answer(
        f"🎉 Спасибо за оплату! Подписка активна до <b>{sub.fmt_until(to_db(until))}</b>.",
        reply_markup=main_menu(),
    )
    amount = f"{payment.total_amount} ⭐" if is_stars else f"{payment.total_amount // 100} сум"
    who = f"@{db_user['username']}" if db_user.get("username") else str(db_user["tg_id"])
    try:
        await bot.send_message(settings.admin_id, f"💰 Оплата: {who} — {amount}, до {sub.fmt_until(to_db(until))}")
    except TelegramAPIError:
        pass
