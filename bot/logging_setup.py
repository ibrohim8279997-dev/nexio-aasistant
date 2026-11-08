"""Логирование с вырезанием секретов."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_FILTERS: list["RedactSecretsFilter"] = []


def register_secret(value: str) -> None:
    """Добавить секрет, появившийся во время работы (например, токен нового агента)."""
    for flt in _FILTERS:
        flt.add(value)


class RedactSecretsFilter(logging.Filter):
    """Заменяет секреты на *** в тексте сообщения и в traceback."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]
        _FILTERS.append(self)

    def add(self, value: str) -> None:
        if value and len(value) >= 8 and value not in self._secrets:
            self._secrets.append(value)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — сломанный формат не должен ронять логирование
            return True
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info:
            record.exc_text = self._redact(logging.Formatter().formatException(record.exc_info))
        return True


def setup_logging(level: str, secrets: list[str], log_dir: Path, name: str = "bot") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = RedactSecretsFilter(secrets)

    stream = logging.StreamHandler()
    file_handler = RotatingFileHandler(log_dir / f"{name}.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    root = logging.getLogger()
    root.handlers.clear()
    for handler in (stream, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        root.addHandler(handler)
    root.setLevel(level)

    # Расход токенов Groq — отдельный файл logs/usage-<агент>.log (у каждого процесса свой)
    usage = RotatingFileHandler(log_dir / f"usage-{name}.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    usage.setFormatter(formatter)
    usage.addFilter(redact)
    usage_logger = logging.getLogger("usage")
    usage_logger.handlers.clear()
    usage_logger.addHandler(usage)
    usage_logger.setLevel(logging.INFO)
    usage_logger.propagate = False

    # Шумные библиотеки
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
