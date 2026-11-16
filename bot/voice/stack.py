"""Интерфейс SIP-стека и звонка. Логика звонка (bot/voice/session.py) знает только эти типы, не pjsua2.

Все методы обработчика вызываются в потоке asyncio (стек сам переносит события из потоков pjsip).
Аудио: PCM 16 бит, 16 кГц, моно, кадры по 20 мс (640 байт)."""
from __future__ import annotations

from typing import Protocol

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2  # 640


class SipCall(Protocol):
    call_id: str
    remote: str  # адрес собеседника (IP)
    direction: str  # "in" | "out"

    def send_audio(self, pcm: bytes) -> None:
        """Поставить звук в очередь воспроизведения в звонок."""

    def clear_audio(self) -> None:
        """Сбросить непроигранное (перебивание)."""

    def pending_ms(self) -> int:
        """Сколько миллисекунд звука ещё не проиграно."""

    def hangup(self) -> None: ...


class CallHandler(Protocol):
    def on_early(self) -> None:
        """Исходящий: удалённая сторона звонит (180/183)."""

    def on_connected(self) -> None: ...

    def on_audio(self, frame: bytes) -> None: ...

    def on_ended(self, reason: str) -> None: ...


class SipStack(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def call_out(self, uri: str, handler: CallHandler) -> SipCall: ...


def split_frames(pcm: bytes) -> list[bytes]:
    """Нарезка PCM на кадры по 20 мс, последний добивается тишиной."""
    frames = []
    for i in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[i : i + FRAME_BYTES]
        frames.append(chunk + b"\x00" * (FRAME_BYTES - len(chunk)))
    return frames
