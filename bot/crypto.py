"""Шифрование секретов, которые бот хранит в SQLite (Fernet). Ключ — только в .env."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from bot.config import ConfigError
from bot.db import Database


class DecryptError(Exception):
    """Не удалось расшифровать: другой ключ или повреждённые данные."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


class Crypto:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError):
            raise ConfigError("ENCRYPTION_KEY некорректен. Сгенерируйте новый: python gen_key.py") from None

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise DecryptError("Не удалось расшифровать значение (сменился ENCRYPTION_KEY?)") from None


class SecretStore:
    """Именованные секреты в таблице secrets, всегда в зашифрованном виде."""

    def __init__(self, db: Database, crypto: Crypto) -> None:
        self._db = db
        self._crypto = crypto

    async def set(self, name: str, value: str) -> None:
        await self._db.set_secret(name, self._crypto.encrypt(value))

    async def get(self, name: str) -> str | None:
        token = await self._db.get_secret(name)
        return self._crypto.decrypt(token) if token else None

    async def names(self) -> list[str]:
        return await self._db.secret_names()

    async def delete(self, name: str) -> bool:
        return await self._db.delete_secret(name)
