"""Расходы: разбор ввода, сохранение, итоги в UZS и USD."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Any

from bot.ai.actions import MAX_AMOUNT, MAX_CATEGORY_LEN, MAX_NOTE_LEN
from bot.db import Database
from bot.services.rates import RateInfo, RatesService
from bot.timeutil import TZ, from_db, now_local, now_utc, period_bounds, to_db

PERIOD_TITLES = {"day": "сегодня", "week": "7 дней", "month": "30 дней"}


class ExpenseInputError(ValueError):
    """Некорректный ввод расхода."""


@dataclass
class ParsedExpense:
    amount: float
    currency: str
    category: str
    note: str | None


_NUM_RE = re.compile(
    r"(?P<usd>\$)?\s*(?P<num>\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s*(?P<mul>млн|mln|тысяч[аи]?|тыс\.?|ming|к|k)?(?!\w)",
    re.IGNORECASE,
)
_USD_RE = re.compile(r"(?<!\w)(usd|долл\w*|dollar\w*|бакс\w*)(?!\w)|\$", re.IGNORECASE)
_UZS_RE = re.compile(r"(?<!\w)(uzs|сум\w*|сўм|so'?m|sum)(?!\w)", re.IGNORECASE)
_MULTIPLIERS = {"млн": 1e6, "mln": 1e6, "к": 1e3, "k": 1e3, "ming": 1e3}


def validate_amount(amount: float, currency: str) -> float:
    if currency not in MAX_AMOUNT:
        raise ExpenseInputError("Валюта должна быть UZS или USD.")
    if not amount or amount <= 0:
        raise ExpenseInputError("Сумма должна быть больше нуля.")
    if amount > MAX_AMOUNT[currency]:
        raise ExpenseInputError("Слишком большая сумма.")
    return round(amount, 2)


def clean_category(category: str | None) -> str:
    category = re.sub(r"\s+", " ", category or "").strip(" ,.-—") or "Другое"
    if len(category) > MAX_CATEGORY_LEN:
        raise ExpenseInputError(f"Категория длиннее {MAX_CATEGORY_LEN} символов.")
    return category[:1].upper() + category[1:]


def clean_note(note: str | None) -> str | None:
    note = re.sub(r"\s+", " ", note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise ExpenseInputError(f"Заметка длиннее {MAX_NOTE_LEN} символов.")
    return note or None


def parse_expense_input(raw: str) -> ParsedExpense:
    """«такси 25000», «25 000 сум еда обед», «12 usd кофе», «$5 такси», «1,5 млн аренда»."""
    m = _NUM_RE.search(raw or "")
    if not m:
        raise ExpenseInputError("Не нашёл сумму. Пример: «такси 25000» или «кофе 3 usd».")
    amount = float(re.sub(r"[  ]", "", m.group("num")).replace(",", "."))
    mul = (m.group("mul") or "").lower().rstrip(".")
    amount *= _MULTIPLIERS.get(mul, 1e3 if mul.startswith("тыс") else 1)
    rest = raw[: m.start()] + " " + raw[m.end() :]

    currency = "USD" if m.group("usd") or _USD_RE.search(rest) else "UZS"
    rest = _UZS_RE.sub(" ", _USD_RE.sub(" ", rest))
    words = rest.split()
    category = words[0] if words else None
    note = " ".join(words[1:]) if len(words) > 1 else None
    return ParsedExpense(validate_amount(amount, currency), currency, clean_category(category), clean_note(note))


def fmt_money(amount: float, currency: str) -> str:
    if currency == "USD":
        return f"${amount:,.2f}".replace(",", " ")
    return f"{amount:,.0f} сум".replace(",", " ")


async def create_expense(
    db: Database, rates: RatesService, user_id: int, amount: float, currency: str, category: str, note: str | None
) -> tuple[int, RateInfo | None]:
    amount = validate_amount(amount, currency)
    category = clean_category(category)
    note = clean_note(note)
    info = await rates.try_get()
    expense_id = await db.add_expense(
        user_id, amount, currency, category, note, info.rate if info else None, to_db(now_utc())
    )
    return expense_id, info


def created_text(amount: float, currency: str, category: str, note: str | None, info: RateInfo | None) -> str:
    lines = [f"💸 Записал: <b>{fmt_money(amount, currency)}</b> — {escape(category)}"]
    if note:
        lines.append(f"📝 {escape(note)}")
    if info:
        other = "UZS" if currency == "USD" else "USD"
        converted = amount * info.rate if currency == "USD" else amount / info.rate
        lines.append(f"≈ {fmt_money(converted, other)} (курс ЦБ {info.rate:,.2f})".replace(",", " "))
        if info.warning():
            lines.append(info.warning())
    else:
        lines.append("⚠️ Курс ЦБ недоступен — конвертация будет по курсу позже.")
    return "\n".join(lines)


@dataclass
class Totals:
    count: int
    native: dict[str, float]  # сумма в исходной валюте
    uzs: float | None  # всё в сумах (если есть курс)
    usd: float | None  # всё в долларах (если есть курс)
    by_category_uzs: dict[str, float]


def compute_totals(rows: list[dict[str, Any]], fallback_rate: float | None) -> Totals:
    native: dict[str, float] = defaultdict(float)
    by_cat: dict[str, float] = defaultdict(float)
    uzs = usd = 0.0
    convertible = True
    for r in rows:
        amount, cur = float(r["amount"]), r["currency"]
        native[cur] += amount
        rate = r.get("rate_used") or fallback_rate
        if rate:
            in_uzs = amount * rate if cur == "USD" else amount
            uzs += in_uzs
            usd += amount / rate if cur == "UZS" else amount
            by_cat[r["category"]] += in_uzs
        elif cur == "UZS":
            uzs += amount
            by_cat[r["category"]] += amount
            convertible = False
        else:
            convertible = False
    return Totals(
        count=len(rows),
        native=dict(native),
        uzs=uzs if convertible else None,
        usd=usd if convertible else None,
        by_category_uzs=dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
    )


async def summary_text(db: Database, rates: RatesService, user_id: int, period: str) -> str:
    today: date = now_local().date()
    start, end = period_bounds(period, today)
    rows = await db.expenses_between(user_id, start, end)
    title = f"💸 <b>Расходы за {PERIOD_TITLES[period]}</b>"
    if not rows:
        return f"{title}\n\nРасходов нет."

    info = await rates.try_get()
    totals = compute_totals(rows, info.rate if info else None)
    lines = [title, ""]
    if totals.uzs is not None and totals.usd is not None:
        lines.append(f"Итого: <b>{fmt_money(totals.uzs, 'UZS')}</b> ≈ <b>{fmt_money(totals.usd, 'USD')}</b>")
    else:
        parts = [fmt_money(v, c) for c, v in totals.native.items()]
        lines.append("Итого: <b>" + " + ".join(parts) + "</b> (курс недоступен)")
    if len(totals.native) > 1:
        lines.append("В т.ч.: " + ", ".join(fmt_money(v, c) for c, v in totals.native.items()))
    lines.append(f"Записей: {totals.count}")

    if totals.by_category_uzs:
        lines.append("\n<b>По категориям</b>")
        total = sum(totals.by_category_uzs.values()) or 1
        for cat, value in list(totals.by_category_uzs.items())[:10]:
            lines.append(f"• {escape(cat)}: {fmt_money(value, 'UZS')} ({value / total:.0%})")
    if info and info.warning():
        lines.append("\n" + info.warning())
    return "\n".join(lines)


def rate_text(info: RateInfo) -> str:
    text = (
        f"💱 <b>Курс ЦБ Узбекистана</b>\n1 USD = <b>{info.rate:,.2f}</b> сум\n"
        f"Дата курса: {datetime_ru(info.date)}"
    ).replace(",", " ")
    if info.warning():
        text += "\n" + info.warning()
    return text


def datetime_ru(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def daily_uzs(rows: list[dict[str, Any]], fallback_rate: float | None, days: list[date]) -> list[float]:
    """Сумма расходов в сумах по локальным дням (USD без курса пропускаются)."""
    index = {d: i for i, d in enumerate(days)}
    values = [0.0] * len(days)
    for r in rows:
        created = from_db(r["created_at"])
        i = index.get(created.astimezone(TZ).date()) if created else None
        if i is None:
            continue
        rate = r.get("rate_used") or fallback_rate
        if r["currency"] == "UZS":
            values[i] += float(r["amount"])
        elif rate:
            values[i] += float(r["amount"]) * rate
    return values
