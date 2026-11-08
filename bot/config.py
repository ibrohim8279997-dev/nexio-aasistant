"""Чтение и проверка настроек из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Ошибка конфигурации: бот не может стартовать."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана переменная {name} в .env")
    return value


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"Не задана переменная {name} в .env")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Переменная {name} должна быть целым числом") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    groq_api_key: str
    admin_id: int
    encryption_key: str
    text_model: str
    stt_model: str
    db_path: Path
    click_enabled: bool
    subscription_enabled: bool
    search_enabled: bool
    click_provider_token: str
    rate_limit_per_min: int
    sub_price_xtr: int
    sub_days: int
    click_price_uzs: int
    log_level: str

    @property
    def secret_values(self) -> list[str]:
        """Значения, которые нельзя выводить в логи (включая токены агентов *_TOKEN из окружения)."""
        values = [self.bot_token, self.groq_api_key, self.encryption_key, self.click_provider_token]
        values += [v for k, v in os.environ.items() if k.endswith("_TOKEN") and v]
        return [v for v in values if v]


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or BASE_DIR / ".env")

    db_path = Path(os.getenv("DB_PATH", "").strip() or "data/bot.db")
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    settings = Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        groq_api_key=_required("GROQ_API_KEY"),
        admin_id=_int("ADMIN_ID"),
        encryption_key=_required("ENCRYPTION_KEY"),
        text_model=os.getenv("TEXT_MODEL", "").strip(),
        stt_model=os.getenv("STT_MODEL", "").strip(),
        db_path=db_path,
        click_enabled=_bool("CLICK_ENABLED", False),
        subscription_enabled=_bool("SUBSCRIPTION_ENABLED", False),
        search_enabled=_bool("SEARCH_ENABLED", True),
        click_provider_token=os.getenv("CLICK_PROVIDER_TOKEN", "").strip(),
        rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 30),
        sub_price_xtr=_int("SUB_PRICE_XTR", 250),
        sub_days=_int("SUB_DAYS", 30),
        click_price_uzs=_int("CLICK_PRICE_UZS", 49000),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
    if settings.admin_id <= 0:
        raise ConfigError("ADMIN_ID должен быть вашим числовым Telegram ID")
    if settings.sub_price_xtr <= 0 or settings.sub_days <= 0:
        raise ConfigError("SUB_PRICE_XTR и SUB_DAYS должны быть больше нуля")
    return settings
