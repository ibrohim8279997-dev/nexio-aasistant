"""Фейковый Bot для прогона апдейтов через диспетчер без сети."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

FAKE_TOKEN = "123456:" + "A" * 35


class FakeBot(Bot):
    def __init__(self) -> None:
        super().__init__(FAKE_TOKEN)
        self.calls: list[TelegramMethod[Any]] = []
        self._mid = 100

    async def __call__(self, method: TelegramMethod[Any], request_timeout: int | None = None) -> Any:
        self.calls.append(method)
        name = type(method).__name__
        if name in {"SendMessage", "EditMessageText", "SendPhoto", "SendInvoice", "SendChatAction"}:
            if name == "SendChatAction":
                return True
            self._mid += 1
            chat_id = getattr(method, "chat_id", 1) or 1
            return Message(
                message_id=self._mid,
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        return True

    def texts(self) -> list[str]:
        return [str(getattr(c, "text", "") or getattr(c, "caption", "") or "") for c in self.calls]

    def methods(self) -> list[str]:
        return [type(c).__name__ for c in self.calls]


_update_id = 0


def _next_id() -> int:
    global _update_id
    _update_id += 1
    return _update_id


def make_user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name=f"U{uid}")


def message_update(uid: int, text: str | None = None, **extra: Any) -> Update:
    msg = Message(
        message_id=_next_id(),
        date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type="private"),
        from_user=make_user(uid),
        text=text,
        **extra,
    )
    return Update(update_id=_next_id(), message=msg)


def callback_update(uid: int, data: str) -> Update:
    msg = Message(message_id=_next_id(), date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"), text="x")
    call = CallbackQuery(id=str(_next_id()), from_user=make_user(uid), chat_instance="ci", data=data, message=msg)
    return Update(update_id=_next_id(), callback_query=call)
