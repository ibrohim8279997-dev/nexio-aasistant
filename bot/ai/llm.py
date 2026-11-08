"""Общий слой LLM: агенты получают клиента по провайдеру и моделям из своего конфига.

Сейчас подключён только Groq (бесплатный тариф). Новый провайдер — новый класс с тем же интерфейсом
(chat, chat_json, transcribe, close) и строка в PROVIDERS.
"""
from __future__ import annotations

from typing import Any, Protocol

from bot.ai.groq_client import ChatResult, GroqClient
from bot.config import ConfigError, Settings


class LLMClient(Protocol):
    text_model: str
    stt_model: str

    async def chat(
        self, messages: list[dict[str, str]], *, json_mode: bool = False, search: bool = False,
        max_tokens: int = 2048, agent: str = "",
    ) -> ChatResult: ...

    async def chat_json(self, messages: list[dict[str, str]], max_tokens: int = 2048) -> str: ...

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str: ...

    async def close(self) -> None: ...


def _groq(settings: Settings, text_model: str, stt_model: str) -> GroqClient:
    return GroqClient(settings.groq_api_key, text_model, stt_model)


PROVIDERS: dict[str, Any] = {"groq": _groq}


def make_llm(settings: Settings, provider: str, text_model: str | None, stt_model: str | None) -> LLMClient:
    factory = PROVIDERS.get((provider or "groq").lower())
    if factory is None:
        raise ConfigError(f"Неизвестный провайдер LLM: {provider}. Доступно: {', '.join(PROVIDERS)}")
    return factory(settings, text_model or settings.text_model, stt_model or "")
