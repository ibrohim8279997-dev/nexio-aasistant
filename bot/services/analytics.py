"""Аналитика доходов, расходов и задач для Mini App. Все функции принимают owner (users.id) и читают только его данные."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from bot.timeutil import TZ, from_db, local_midnight, now_local, to_db


def to_uzs(row: dict[str, Any], fallback: float | None) -> float | None:
    if row["currency"] == "UZS":
        return float(row["amount"])
    rate = row.get("rate_used") or fallback
    return float(row["amount"]) * rate if rate else None


def to_usd(row: dict[str, Any], fallback: float | None) -> float | None:
    if row["currency"] == "USD":
        return float(row["amount"])
    rate = row.get("rate_used") or fallback
    return float(row["amount"]) / rate if rate else None


def _bucket_start(day: date, group: str) -> date:
    if group == "week":
        return day - timedelta(days=day.weekday())
    if group == "month":
        return day.replace(day=1)
    return day


def _next_bucket(start: date, group: str) -> date:
    if group == "week":
        return start + timedelta(days=7)
    if group == "month":
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start + timedelta(days=1)


def bucket_starts(group: str, count: int, today: date) -> list[date]:
    starts = [_bucket_start(today, group)]
    while len(starts) < count:
        prev = starts[0] - timedelta(days=1)
        starts.insert(0, _bucket_start(prev, group))
    return starts


async def finance_series(db: Any, owner: int, group: str, count: int, fallback_rate: float | None) -> dict[str, Any]:
    """Динамика по дням/неделям/месяцам: доходы и расходы в UZS и USD."""
    if group not in ("day", "week", "month"):
        raise ValueError("group")
    count = max(1, min(count, 60))
    today = now_local().date()
    starts = bucket_starts(group, count, today)
    start_utc = to_db(local_midnight(starts[0]))
    end_utc = to_db(local_midnight(today + timedelta(days=1)))
    rows = await db.expenses_between(owner, start_utc, end_utc, None)
    points = [{"start": s.isoformat(), "income_uzs": 0.0, "expense_uzs": 0.0, "income_usd": 0.0, "expense_usd": 0.0} for s in starts]
    index = {s: i for i, s in enumerate(starts)}
    for r in rows:
        created = from_db(r["created_at"])
        if created is None:
            continue
        i = index.get(_bucket_start(created.astimezone(TZ).date(), group))
        if i is None:
            continue
        key = "income" if r["kind"] == "income" else "expense"
        uzs, usd = to_uzs(r, fallback_rate), to_usd(r, fallback_rate)
        if uzs is not None:
            points[i][f"{key}_uzs"] += uzs
        if usd is not None:
            points[i][f"{key}_usd"] += usd
    for p in points:
        for k in ("income_uzs", "expense_uzs", "income_usd", "expense_usd"):
            p[k] = round(p[k], 2)
    return {"group": group, "points": points}


async def finance_summary(db: Any, owner: int, period: str, fallback_rate: float | None) -> dict[str, Any]:
    from bot.timeutil import period_bounds

    start, end = period_bounds(period, now_local().date())
    rows = await db.expenses_between(owner, start, end, None)
    out = {"period": period, "income_uzs": 0.0, "expense_uzs": 0.0, "income_usd": 0.0, "expense_usd": 0.0, "count": len(rows)}
    for r in rows:
        key = "income" if r["kind"] == "income" else "expense"
        uzs, usd = to_uzs(r, fallback_rate), to_usd(r, fallback_rate)
        out[f"{key}_uzs"] += uzs or 0.0
        out[f"{key}_usd"] += usd or 0.0
    for k in ("income_uzs", "expense_uzs", "income_usd", "expense_usd"):
        out[k] = round(out[k], 2)
    out["balance_uzs"] = round(out["income_uzs"] - out["expense_uzs"], 2)
    out["balance_usd"] = round(out["income_usd"] - out["expense_usd"], 2)
    return out


async def finance_categories(db: Any, owner: int, period: str, kind: str, fallback_rate: float | None) -> list[dict[str, Any]]:
    from bot.timeutil import period_bounds

    start, end = period_bounds(period, now_local().date())
    totals: dict[str, float] = defaultdict(float)
    for r in await db.expenses_between(owner, start, end, kind):
        totals[r["category"]] += to_uzs(r, fallback_rate) or 0.0
    whole = sum(totals.values()) or 1.0
    return [{"category": c, "uzs": round(v, 2), "share": round(v / whole, 4)}
            for c, v in sorted(totals.items(), key=lambda kv: -kv[1])]


def task_json(t: dict[str, Any]) -> dict[str, Any]:
    due = from_db(t["due_at"])
    return {
        "id": t["id"], "text": t["text"], "has_time": bool(t["has_time"]), "done": bool(t["done_at"]),
        "due": due.astimezone(TZ).isoformat() if due else None,
    }


def money_json(e: dict[str, Any]) -> dict[str, Any]:
    created = from_db(e["created_at"])
    return {"id": e["id"], "kind": e["kind"], "amount": e["amount"], "currency": e["currency"], "category": e["category"],
            "note": e["note"], "created": created.astimezone(TZ).isoformat() if created else None}


async def task_stats(db: Any, owner: int, days: int) -> dict[str, Any]:
    from bot.services import tracker

    days = max(1, min(days, 90))
    data = await tracker.tasks_per_day(db, owner, days)
    since = to_db(local_midnight(now_local().date() - timedelta(days=365)))
    done_days = {from_db(t).astimezone(TZ).date() for t in await db.done_times_since(owner, since)}  # type: ignore[union-attr]
    total, done = sum(data.total), sum(data.done)
    return {
        "days": [{"date": d.isoformat(), "done": a, "total": b} for d, a, b in zip(data.days, data.done, data.total)],
        "done": done, "total": total, "percent": round(done / total * 100) if total else None,
        "streak": tracker.compute_streak(done_days, now_local().date()),
    }


async def advice_input(db: Any, owner: int, fallback_rate: float | None) -> dict[str, Any] | None:
    """Короткая сводка за 30 дней для рекомендаций (без личных заметок). None — данных нет, ИИ не зовём."""
    summary = await finance_summary(db, owner, "month", fallback_rate)
    stats = await task_stats(db, owner, 14)
    if not summary["count"] and not stats["total"]:
        return None
    return {
        "income_uzs": summary["income_uzs"], "expense_uzs": summary["expense_uzs"], "balance_uzs": summary["balance_uzs"],
        "top_expense_categories": (await finance_categories(db, owner, "month", "expense", fallback_rate))[:5],
        "top_income_categories": (await finance_categories(db, owner, "month", "income", fallback_rate))[:3],
        "tasks_done": stats["done"], "tasks_total": stats["total"], "tasks_percent": stats["percent"], "streak_days": stats["streak"],
    }
