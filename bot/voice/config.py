"""Настройки голосового звонка из .env. Логин и пароль SIP — только оттуда, не из кода."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

from dotenv import load_dotenv

from bot.config import BASE_DIR, ConfigError

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")  # адреса Tailscale (CGNAT)
MIN_PASSWORD_LEN = 8


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return default if not raw else raw in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name}: от {low} до {high}")
    return value


@dataclass(frozen=True)
class VoiceConfig:
    sip_user: str
    sip_password: str
    sip_port: int
    bind_address: str  # адрес Tailscale: единственный сетевой адрес, на котором слушает SIP, кроме localhost
    callback_uri: str  # куда звонит Кимо: sip:user@100.x.x.x:порт (Linphone)
    record_calls: bool  # запись звука на диск выключена, пока владелец не включит флаг
    max_call_seconds: int
    idle_seconds: int
    answer_wait_seconds: int  # сколько ждём ответ Кимо на фразу
    outbound_ring_seconds: int
    voice_ru: str
    voice_uz: str
    realm: str = "kimo"

    @classmethod
    def from_env(cls, need_inbound: bool = True) -> VoiceConfig:
        load_dotenv(BASE_DIR / ".env")
        user = os.getenv("SIP_USER", "").strip()
        password = os.getenv("SIP_PASSWORD", "")
        bind = os.getenv("SIP_BIND_ADDRESS", "").strip()
        if need_inbound:
            if not user or not password:
                raise ConfigError("Не заданы SIP_USER и SIP_PASSWORD в .env")
            if len(password) < MIN_PASSWORD_LEN:
                raise ConfigError(f"SIP_PASSWORD короче {MIN_PASSWORD_LEN} символов")
            if not bind:
                raise ConfigError("Не задан SIP_BIND_ADDRESS (адрес Tailscale сервера, 100.x.x.x)")
        if bind:
            try:
                ip = ipaddress.ip_address(bind)
            except ValueError as exc:
                raise ConfigError("SIP_BIND_ADDRESS: некорректный IP-адрес") from exc
            if ip not in TAILSCALE_NET:
                raise ConfigError("SIP_BIND_ADDRESS должен быть адресом Tailscale (100.64.0.0/10): слушать на других адресах нельзя")
        voice_ru = os.getenv("VOICE_RU", "svetlana").strip().lower()
        voice_uz = os.getenv("VOICE_UZ", "madina").strip().lower()
        if voice_ru not in ("svetlana", "dmitry") or voice_uz not in ("madina", "sardor"):
            raise ConfigError("VOICE_RU: svetlana|dmitry, VOICE_UZ: madina|sardor")
        return cls(
            sip_user=user,
            sip_password=password,
            sip_port=_int("SIP_PORT", 5060, 1024, 65535),
            bind_address=bind,
            callback_uri=os.getenv("SIP_CALLBACK_URI", "").strip(),
            record_calls=_bool("SIP_RECORD_CALLS", False),
            max_call_seconds=_int("VOICE_MAX_CALL_SECONDS", 900, 30, 7200),
            idle_seconds=_int("VOICE_IDLE_SECONDS", 120, 15, 1800),
            answer_wait_seconds=_int("VOICE_ANSWER_WAIT_SECONDS", 20, 5, 120),
            outbound_ring_seconds=_int("VOICE_RING_SECONDS", 30, 10, 60),
            voice_ru=voice_ru,
            voice_uz=voice_uz,
        )

    @property
    def secret_values(self) -> list[str]:
        return [v for v in (self.sip_password,) if v]
