"""Фоновые задачи APScheduler: напоминания о задачах и об окончании подписки."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import Settings
from bot.db import Database
from bot.keyboards import reminder_kb, subscription_kb
from bot.services import subscription as sub
from bot.timeutil import TZ, now_utc, to_db

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=12)  # если бот был выключен дольше — старые напоминания не шлём


class BotScheduler:
    def __init__(self, bot: Bot, db: Database, settings: Settings, system_db: Database | None = None) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self.system_db = system_db  # для служебной очистки очереди заданий
        self.scheduler = AsyncIOScheduler(timezone=TZ)

    def start(self) -> None:
        common: dict[str, Any] = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 120}
        self.scheduler.add_job(self.task_reminders, "interval", minutes=1, id="task_reminders", **common)
        self.scheduler.add_job(
            self.subscription_reminders, "interval", hours=1, id="sub_reminders",
            next_run_time=datetime.now(TZ) + timedelta(seconds=30), **common,
        )
        if self.system_db is not None:
            self.scheduler.add_job(self.cleanup_queue, "interval", hours=24, id="queue_cleanup", **common)
        self.scheduler.start()
        log.info("Планировщик запущен")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def has_access(self, tg_id: int, sub_until: str | None) -> bool:
        return sub.has_access(tg_id, sub_until, self.settings.admin_id)

    async def _send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool | None:
        """True — доставлено, False — не доставить никогда (бот заблокирован), None — повторить позже."""
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup)
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            return None
        except TelegramForbiddenError:
            return False
        except TelegramNetworkError as exc:
            log.warning("Сеть Telegram недоступна: %s", type(exc).__name__)
            return None
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить сообщение: %s", type(exc).__name__)
            return False

    async def task_reminders(self) -> None:
        try:
            now = now_utc()
            not_before = to_db(now - STALE_AFTER)
            await self.db.skip_stale_reminders(not_before)
            for row in await self.db.due_reminders(to_db(now), not_before):
                if not self.has_access(row["tg_id"], row["sub_until"]):
                    await self.db.mark_reminded(row["id"])
                    continue
                result = await self._send(
                    row["tg_id"], f"⏰ <b>Напоминание</b>\n{escape(row['text'])}", reminder_kb(row["id"])
                )
                if result is None:
                    break  # сеть/флуд-контроль — попробуем в следующую минуту
                await self.db.mark_reminded(row["id"])
        except Exception:  # noqa: BLE001 — фоновая задача не должна падать молча
            log.exception("Ошибка в задаче напоминаний")

    async def subscription_reminders(self) -> None:
        """За 3 дня до конца подписки — одно напоминание с кнопкой оплаты (если продажа включена)."""
        if not self.settings.subscription_enabled:
            return
        try:
            now = now_utc()
            users = await self.db.users_to_remind_sub(to_db(now), to_db(now + timedelta(days=sub.REMIND_DAYS_BEFORE)))
            markup = subscription_kb(
                self.settings.sub_price_xtr, self.settings.click_enabled, self.settings.click_price_uzs
            )
            for user in users:
                if user["tg_id"] == self.settings.admin_id:
                    continue
                left = sub.days_left(user["sub_until"], now)
                text = (
                    f"⭐ Подписка заканчивается {sub.fmt_until(user['sub_until'])} (осталось {left} дн.).\n"
                    "Продлите, чтобы не потерять напоминания и статистику."
                )
                result = await self._send(user["tg_id"], text, markup)
                if result is None:
                    break
                await self.db.mark_sub_notified(user["id"])
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в задаче напоминаний о подписке")

    async def cleanup_queue(self) -> None:
        """Удалить выполненные/упавшие задания шины старше 7 дней."""
        if self.system_db is None:
            return
        try:
            removed = await self.system_db.queue_cleanup(to_db(now_utc() - timedelta(days=7)))
            if removed:
                log.info("Очередь заданий: удалено старых записей: %s", removed)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка очистки очереди заданий")
