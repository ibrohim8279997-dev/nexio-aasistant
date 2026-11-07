"""Курс USD/UZS с cbu.uz (официальный JSON API ЦБ Узбекистана), кэш 1 час, запасной курс из БД."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from bot.db import Database
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/"
CACHE_TTL = 3600  # свежий курс — 1 час
STALE_RETRY = 300  # если cbu.uz недоступен, пробуем снова не чаще раза в 5 минут


class RateUnavailableError(Exception):
    """Курса нет ни с сайта, ни в кэше."""


@dataclass(frozen=True)
class RateInfo:
    rate: float  # сумов за 1 USD
    date: str  # дата курса, YYYY-MM-DD
    stale: bool = False  # True — сайт недоступен, взят последний сохранённый курс

    def warning(self) -> str:
        if not self.stale:
            return ""
        return f"⚠️ cbu.uz недоступен, использую последний сохранённый курс ({_ru_date(self.date)})."


def _ru_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso


def parse_cbu(payload: Any) -> tuple[float, str]:
    """Ответ cbu.uz -> (курс за 1 USD, дата YYYY-MM-DD)."""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, dict) and item.get("Ccy") == "USD":
            rate = float(str(item["Rate"]).replace(",", "."))
            nominal = float(item.get("Nominal") or 1)
            day = datetime.strptime(item["Date"], "%d.%m.%Y").strftime("%Y-%m-%d")
            if rate <= 0 or nominal <= 0:
                raise ValueError("некорректный курс")
            return rate / nominal, day
    raise ValueError("в ответе нет USD")


def convert(amount: float, from_cur: str, to_cur: str, rate: float) -> float:
    """Конвертация UZS <-> USD по курсу `rate` (сумов за 1 USD)."""
    if from_cur == to_cur:
        return amount
    if rate <= 0:
        raise ValueError("курс должен быть больше нуля")
    if from_cur == "USD" and to_cur == "UZS":
        return amount * rate
    if from_cur == "UZS" and to_cur == "USD":
        return amount / rate
    raise ValueError(f"неизвестная пара {from_cur}->{to_cur}")


class RatesService:
    def __init__(self, db: Database, client: httpx.AsyncClient | None = None) -> None:
        self._db = db
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True)
        self._cache: RateInfo | None = None
        self._cache_until = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    async def get_usd_rate(self) -> RateInfo:
        async with self._lock:
            now = time.monotonic()
            if self._cache and now < self._cache_until:
                return self._cache
            try:
                response = await self._http.get(CBU_URL)
                response.raise_for_status()
                rate, day = parse_cbu(response.json())
                await self._db.save_rate(day, rate, to_db(now_utc()))
                info = RateInfo(rate=rate, date=day)
                ttl = CACHE_TTL
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.warning("Не удалось получить курс с cbu.uz: %s", type(exc).__name__)
                row = await self._db.last_rate()
                if row is None:
                    raise RateUnavailableError("курс недоступен") from exc
                info = RateInfo(rate=float(row["usd_uzs"]), date=row["date"], stale=True)
                ttl = STALE_RETRY
            self._cache, self._cache_until = info, now + ttl
            return info

    async def try_get(self) -> RateInfo | None:
        try:
            return await self.get_usd_rate()
        except RateUnavailableError:
            return None
