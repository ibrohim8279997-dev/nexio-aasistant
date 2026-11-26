"""HTTP API для Mini App (aiohttp). Аутентификация — initData (HMAC), роль и права — из БД. Все данные фильтруются по owner."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from bot.agents.health import effective_status
from bot.agents.registry import NAME_RE
from bot.api.auth import AuthError, RateLimiter, validate_init_data
from bot.services import analytics, broadcast as bcast, business as biz, grants, perms as perms_svc
from bot.services import expenses as exp_svc
from bot.services import tasks as tasks_svc
from bot.timeutil import TZ, from_db, local_midnight, now_local, now_utc, to_db

log = logging.getLogger("api")

MAX_BODY = 64 * 1024
HEAVY_PATHS = ("/api/assistant", "/api/advice")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


@dataclass
class ApiContext:
    settings: Any
    db: Any
    bot_token: str
    bus: Any = None  # Bus(db, "miniapp"): связь с Кимо
    rates: Any = None
    notify: Callable[[int, str], Awaitable[None]] | None = None
    webapp_dir: Path | None = None
    bot_username: str = ""
    kimo_wait: float = 45.0
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(120))
    heavy_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(10))
    auth_fail_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(20))
    clock: Callable[[], float] | None = None
    advice_locks: dict[int, asyncio.Lock] = field(default_factory=dict)


routes = web.RouteTableDef()


def ctx_of(request: web.Request) -> ApiContext:
    return request.app["ctx"]


def me(request: web.Request) -> dict[str, Any]:
    return request["user"]


def require(request: web.Request, perm: str) -> None:
    if perm not in request["perms"]:
        raise ApiError(403, "forbidden", "Эта функция вам недоступна")


def require_admin(request: web.Request) -> None:
    if not request["is_admin"]:
        raise ApiError(403, "forbidden", "Только для владельца")


async def body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_json", "Некорректный JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "Ожидался объект")
    return data


def int_param(value: Any, name: str, lo: int | None = None, hi: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_param", f"Некорректный параметр {name}") from None
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        raise ApiError(400, "bad_param", f"Параметр {name} вне диапазона")
    return number


def iso_local(value: str | None) -> str | None:
    dt = from_db(value)
    return dt.astimezone(TZ).isoformat() if dt else None


# ---------- middleware ----------

@web.middleware
async def headers_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if not request.path.startswith("/api/"):
        response.headers.setdefault("Content-Security-Policy", CSP)
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as exc:
        return web.json_response({"error": exc.code, "message": exc.message}, status=exc.status)
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — наружу без деталей, в лог — только тип
        log.error("Ошибка в %s %s: %s", request.method, request.path, type(exc).__name__, exc_info=exc)
        return web.json_response({"error": "internal", "message": "Внутренняя ошибка"}, status=500)


def client_ip(request: web.Request) -> str:
    remote = request.remote or "?"
    if remote in ("127.0.0.1", "::1"):  # за Tailscale Funnel/прокси реальный адрес в X-Forwarded-For
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return forwarded or remote
    return remote


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)
    ctx = ctx_of(request)
    ip = client_ip(request)
    header = request.headers.get("Authorization", "")
    init_data = header[4:] if header.startswith("tma ") else ""
    try:
        tg_user = validate_init_data(init_data, ctx.bot_token)
    except AuthError:
        if not ctx.auth_fail_limiter.allow(ip):
            raise ApiError(429, "rate_limited", "Слишком много попыток") from None
        raise ApiError(401, "unauthorized", "Откройте Mini App из Telegram") from None
    if not ctx.limiter.allow(tg_user["id"]):
        raise ApiError(429, "rate_limited", "Слишком много запросов, подождите минуту")
    if request.path in HEAVY_PATHS and not ctx.heavy_limiter.allow(tg_user["id"]):
        raise ApiError(429, "rate_limited", "Слишком частые запросы к ИИ")
    name = tg_user.get("first_name") or tg_user.get("username") or ""
    user = await ctx.db.get_or_create_user(tg_user["id"], tg_user.get("username"), name, to_db(now_utc()))
    request["tg_user"] = tg_user
    request["user"] = user
    request["is_admin"] = perms_svc.is_admin(user["tg_id"], ctx.settings.admin_id)
    if not await grants.has_access(ctx.db, user, ctx.settings):
        raise ApiError(403, "no_access", f"Доступ не открыт. Ваш ID: {user['tg_id']}")
    request["perms"] = await perms_svc.effective_perms(ctx.db, user, ctx.settings.admin_id)
    return await handler(request)


# ---------- профиль и настройки ----------

SETTING_KEYS = ("accent", "lang", "tz", "broadcast_max_per_day")


async def settings_of(ctx: ApiContext, user: dict[str, Any]) -> dict[str, Any]:
    stored = await ctx.db.user_settings(user["id"])
    return {
        "accent": stored.get("accent") or "#19d3c5",
        "lang": stored.get("lang") or user.get("lang") or "ru",
        "tz": stored.get("tz") or bcast.DEFAULT_TZ,
        "broadcast_max_per_day": int(stored.get("broadcast_max_per_day") or bcast.DEFAULT_MAX_PER_DAY),
    }


@routes.get("/health")
async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@routes.get("/api/me")
async def api_me(request: web.Request) -> web.Response:
    ctx, user = ctx_of(request), me(request)
    until = await grants.active_until(ctx.db, user)
    return web.json_response({
        "id": user["tg_id"], "name": user.get("first_name") or "", "role": "admin" if request["is_admin"] else "user",
        "perms": sorted(request["perms"]), "access_until": until.isoformat() if until else None,
        "settings": await settings_of(ctx, user),
    })


@routes.get("/api/settings")
async def get_settings(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    out: dict[str, Any] = {"settings": await settings_of(ctx, me(request))}
    if request["is_admin"]:
        out["payments"] = {
            "subscription_enabled": ctx.settings.subscription_enabled, "click_enabled": ctx.settings.click_enabled,
            "price_xtr": ctx.settings.sub_price_xtr, "price_uzs": ctx.settings.click_price_uzs, "days": ctx.settings.sub_days,
        }
        names = set(await ctx.db.secret_names())
        tokens = []
        for a in await ctx.db.list_agents():
            env_name = (a.get("config") or {}).get("token_env") or ""
            is_service = (a.get("config") or {}).get("kind") == "service"
            if is_service:
                continue
            tokens.append({"agent": a["name"], "set": bool(env_name and os.getenv(env_name)) or f"bot_token:{a['name']}" in names})
        out["tokens"] = tokens  # только факт «задан», значения не отдаются никогда
        out["groq_key_set"] = bool(ctx.settings.groq_api_key)
    return web.json_response(out)


@routes.put("/api/settings")
async def put_settings(request: web.Request) -> web.Response:
    ctx, user = ctx_of(request), me(request)
    data = await body(request)
    for key, value in data.items():
        if key not in SETTING_KEYS:
            raise ApiError(400, "bad_param", f"Неизвестная настройка {key}")
        if key == "accent":
            if not isinstance(value, str) or not ACCENT_RE.match(value):
                raise ApiError(400, "bad_param", "Цвет в формате #RRGGBB")
        elif key == "lang":
            if value not in ("ru", "uz"):
                raise ApiError(400, "bad_param", "Язык: ru или uz")
        elif key == "tz":
            try:
                bcast.check_tz(str(value))
            except bcast.BroadcastError as exc:
                raise ApiError(400, "bad_param", str(exc)) from None
        elif key == "broadcast_max_per_day":
            value = int_param(value, key, 1, 5)
        await ctx.db.set_user_setting(user["id"], key, str(value))
    return web.json_response({"settings": await settings_of(ctx, user)})


# ---------- главная ----------

@routes.get("/api/home")
async def home(request: web.Request) -> web.Response:
    ctx, user = ctx_of(request), me(request)
    owner, perms = user["id"], request["perms"]
    out: dict[str, Any] = {"name": user.get("first_name") or ""}
    today = now_local().date()
    if "tasks" in perms:
        rows = await ctx.db.task_list_range(owner, *_day_range(today))
        out["tasks"] = {"done": sum(1 for r in rows if r["done_at"]), "total": len(rows)}
    if "finance" in perms:
        info = await ctx.rates.try_get()
        out["finance"] = await analytics.finance_summary(ctx.db, owner, "day", info.rate if info else None)
    if request["is_admin"]:
        agents = [{"name": a["name"], "display_name": a.get("display_name") or a["name"],
                   "status": effective_status(a.get("last_seen"), a.get("status")), "enabled": bool(a["enabled"])}
                  for a in await ctx.db.list_agents()]
        out["agents"] = agents
        out["pulse"] = sum(1 for a in agents if a["status"] in ("online", "busy"))
    if "business" in perms:
        conns = await ctx.db.bconn_list(owner)
        out["business"] = {"connected": any(c["is_enabled"] and c["can_reply"] for c in conns),
                           "ai_enabled": all(c["ai_enabled"] for c in conns) if conns else False}
    return web.json_response(out)


def _day_range(day: date) -> tuple[str, str]:
    from bot.timeutil import day_bounds

    return day_bounds(day)


# ---------- задачи и напоминания ----------

@routes.get("/api/tasks")
async def list_tasks(request: web.Request) -> web.Response:
    require(request, "tasks")
    ctx, owner = ctx_of(request), me(request)["id"]
    day = request.query.get("day", "today")
    today = now_local().date()
    if day == "all":
        rows = await ctx.db.open_tasks(owner, 100)
    elif day in ("today", "tomorrow"):
        target = today if day == "today" else today + timedelta(days=1)
        rows = await ctx.db.task_list_range(owner, *_day_range(target))
        if day == "today":  # просроченные открытые — вместе с сегодняшними
            overdue = [t for t in await ctx.db.open_tasks(owner, 100) if t["due_at"] and t["due_at"] < _day_range(today)[0]]
            rows = overdue + rows
    else:
        raise ApiError(400, "bad_param", "day: today, tomorrow или all")
    return web.json_response({"tasks": [analytics.task_json(t) for t in rows]})


def parse_due(data: dict[str, Any], need_time: bool) -> tuple[datetime, bool]:
    try:
        day = date.fromisoformat(data["date"]) if data.get("date") else None
        at = time.fromisoformat(data["time"]) if data.get("time") else None
    except ValueError:
        raise ApiError(400, "bad_param", "Дата ГГГГ-ММ-ДД, время ЧЧ:ММ") from None
    if need_time and at is None:
        raise ApiError(400, "bad_param", "Для напоминания нужно время")
    return tasks_svc.resolve_due(day, at, now_local())


@routes.post("/api/tasks")
async def create_task(request: web.Request) -> web.Response:
    require(request, "tasks")
    ctx, owner = ctx_of(request), me(request)["id"]
    data = await body(request)
    due, has_time = parse_due(data, False)
    try:
        task_id = await tasks_svc.create_task(ctx.db, owner, str(data.get("text", "")), due, has_time)
    except tasks_svc.TaskInputError as exc:
        raise ApiError(400, "bad_param", str(exc)) from None
    return web.json_response({"id": task_id}, status=201)


@routes.post("/api/tasks/{id}/done")
async def done_task(request: web.Request) -> web.Response:
    require(request, "tasks")
    ctx, owner = ctx_of(request), me(request)["id"]
    ok = await ctx.db.complete_task(owner, int_param(request.match_info["id"], "id", 1), to_db(now_utc()))
    if not ok:
        raise ApiError(404, "not_found", "Задача не найдена")
    return web.json_response({"ok": True})


@routes.delete("/api/tasks/{id}")
async def delete_task(request: web.Request) -> web.Response:
    require(request, "tasks")
    ok = await ctx_of(request).db.delete_task(me(request)["id"], int_param(request.match_info["id"], "id", 1))
    if not ok:
        raise ApiError(404, "not_found", "Задача не найдена")
    return web.json_response({"ok": True})


@routes.get("/api/tasks/stats")
async def tasks_stats(request: web.Request) -> web.Response:
    require(request, "tasks")
    days = int_param(request.query.get("days", 14), "days", 1, 90)
    return web.json_response(await analytics.task_stats(ctx_of(request).db, me(request)["id"], days))


@routes.get("/api/reminders")
async def list_reminders(request: web.Request) -> web.Response:
    require(request, "reminders")
    now = now_utc()
    rows = await ctx_of(request).db.upcoming_reminders(me(request)["id"], to_db(now), to_db(now + timedelta(days=30)))
    return web.json_response({"reminders": [analytics.task_json(t) for t in rows]})


@routes.post("/api/reminders")
async def create_reminder(request: web.Request) -> web.Response:
    require(request, "reminders")
    ctx, owner = ctx_of(request), me(request)["id"]
    data = await body(request)
    due, has_time = parse_due(data, True)
    try:
        task_id = await tasks_svc.create_task(ctx.db, owner, str(data.get("text", "")), due, has_time)
    except tasks_svc.TaskInputError as exc:
        raise ApiError(400, "bad_param", str(exc)) from None
    return web.json_response({"id": task_id}, status=201)


# ---------- финансы ----------

async def fallback_rate(ctx: ApiContext) -> float | None:
    info = await ctx.rates.try_get()
    return info.rate if info else None


@routes.get("/api/rate")
async def get_rate(request: web.Request) -> web.Response:
    info = await ctx_of(request).rates.try_get()
    return web.json_response({"usd_uzs": info.rate if info else None, "date": info.date if info else None, "source": "cbu.uz"})


@routes.get("/api/finance/summary")
async def fin_summary(request: web.Request) -> web.Response:
    require(request, "finance")
    ctx = ctx_of(request)
    period = request.query.get("period", "month")
    if period not in ("day", "week", "month"):
        raise ApiError(400, "bad_param", "period: day, week, month")
    return web.json_response(await analytics.finance_summary(ctx.db, me(request)["id"], period, await fallback_rate(ctx)))


@routes.get("/api/finance/series")
async def fin_series(request: web.Request) -> web.Response:
    require(request, "finance")
    ctx = ctx_of(request)
    group = request.query.get("group", "day")
    if group not in ("day", "week", "month"):
        raise ApiError(400, "bad_param", "group: day, week, month")
    count = int_param(request.query.get("count", {"day": 14, "week": 8, "month": 6}[group]), "count", 1, 60)
    return web.json_response(await analytics.finance_series(ctx.db, me(request)["id"], group, count, await fallback_rate(ctx)))


@routes.get("/api/finance/categories")
async def fin_categories(request: web.Request) -> web.Response:
    require(request, "finance")
    ctx = ctx_of(request)
    kind = request.query.get("kind", "expense")
    period = request.query.get("period", "month")
    if kind not in ("expense", "income") or period not in ("day", "week", "month"):
        raise ApiError(400, "bad_param", "kind: expense|income, period: day|week|month")
    cats = await analytics.finance_categories(ctx.db, me(request)["id"], period, kind, await fallback_rate(ctx))
    return web.json_response({"kind": kind, "categories": cats})


@routes.get("/api/finance/items")
async def fin_items(request: web.Request) -> web.Response:
    require(request, "finance")
    rows = await ctx_of(request).db.expense_list(me(request)["id"], int_param(request.query.get("limit", 30), "limit", 1, 100))
    return web.json_response({"items": [analytics.money_json(r) for r in rows]})


@routes.post("/api/finance")
async def fin_add(request: web.Request) -> web.Response:
    require(request, "finance")
    ctx = ctx_of(request)
    data = await body(request)
    kind = data.get("kind", "expense")
    if kind not in ("expense", "income"):
        raise ApiError(400, "bad_param", "kind: expense или income")
    try:
        amount = float(data.get("amount"))
        item_id, _ = await exp_svc.create_expense(
            ctx.db, ctx.rates, me(request)["id"], amount, str(data.get("currency", "UZS")).upper(),
            str(data.get("category") or "Другое"), data.get("note"), kind)
    except (TypeError, ValueError) as exc:  # ExpenseInputError тоже ValueError
        raise ApiError(400, "bad_param", str(exc) if isinstance(exc, exp_svc.ExpenseInputError) else "Некорректная сумма") from None
    return web.json_response({"id": item_id}, status=201)


@routes.delete("/api/finance/{id}")
async def fin_delete(request: web.Request) -> web.Response:
    require(request, "finance")
    ok = await ctx_of(request).db.delete_expense(me(request)["id"], int_param(request.match_info["id"], "id", 1))
    if not ok:
        raise ApiError(404, "not_found", "Запись не найдена")
    return web.json_response({"ok": True})


# ---------- AI-рекомендации ----------

@routes.get("/api/advice")
async def get_advice(request: web.Request) -> web.Response:
    """Генерируется раз в сутки на пользователя и язык, дальше отдаётся из кэша."""
    require(request, "assistant")
    ctx, user = ctx_of(request), me(request)
    lang = (await settings_of(ctx, user))["lang"]
    day = now_local().date().isoformat()
    cached = await ctx.db.advice_get(user["id"], day, lang)
    if cached:
        return web.json_response({**json.loads(cached["payload"]), "day": day, "cached": True})
    async with ctx.advice_locks.setdefault(user["id"], asyncio.Lock()):
        cached = await ctx.db.advice_get(user["id"], day, lang)  # пока ждали замок, мог сгенерировать другой запрос
        if cached:
            return web.json_response({**json.loads(cached["payload"]), "day": day, "cached": True})
        stats = await analytics.advice_input(ctx.db, user["id"], await fallback_rate(ctx))
        if stats is None:
            return web.json_response({"tips": [], "empty": True, "day": day})
        try:
            raw = await ask_kimo(ctx, {"type": "advice", "source": "miniapp", "tg_id": user["tg_id"], "lang": lang, "stats": stats},
                                 user["tg_id"], timeout=ctx.kimo_wait)
            data = json.loads(raw)
            if not isinstance(data.get("tips"), list) or not data["tips"]:
                raise ValueError
        except (ApiError, ValueError):
            older = await ctx.db.advice_last(user["id"], lang)  # ИИ недоступен — показываем прошлые советы, не тратя лимит
            if older:
                return web.json_response({**json.loads(older["payload"]), "day": older["day"], "stale": True})
            return web.json_response({"tips": [], "unavailable": True, "day": day})
        await ctx.db.advice_set(user["id"], day, lang, json.dumps({"tips": data["tips"]}, ensure_ascii=False), to_db(now_utc()))
        return web.json_response({"tips": data["tips"], "day": day, "cached": False})


# ---------- связь с Кимо (ассистент, агенты) ----------

async def ask_kimo(ctx: ApiContext, payload: dict[str, Any], tg_id: int, timeout: float) -> str:
    if ctx.bus is None:
        raise ApiError(503, "kimo_unavailable", "Кимо недоступен")
    kimo = await ctx.db.get_agent("kimo")
    if kimo is None or effective_status(kimo.get("last_seen"), kimo.get("status")) == "offline":
        raise ApiError(503, "kimo_unavailable", "Кимо сейчас не запущен")
    task_id = await ctx.bus.send("kimo", {**payload, "reply": False}, depth=1, chat_id=tg_id)
    try:
        task = await ctx.bus.wait_result(task_id, timeout=timeout, interval=0.3)
    except Exception:  # noqa: BLE001
        raise ApiError(504, "kimo_timeout", "Кимо не ответил вовремя") from None
    await ctx.db.queue_mark_delivered(task_id)
    if task["status"] == "failed":
        raise ApiError(502, "kimo_failed", str(task["result"] or "Ошибка").removeprefix("⚠️").strip()[:300])
    return str(task["result"] or "")


@routes.post("/api/assistant")
async def assistant(request: web.Request) -> web.Response:
    require(request, "assistant")
    ctx, user = ctx_of(request), me(request)
    text = str((await body(request)).get("text", "")).strip()
    if not text or len(text) > 1000:
        raise ApiError(400, "bad_param", "Текст до 1000 символов")
    raw = await ask_kimo(ctx, {"type": "user_command", "source": "miniapp", "tg_id": user["tg_id"], "text": text},
                         user["tg_id"], timeout=ctx.kimo_wait)
    try:
        return web.json_response({"text": json.loads(raw)["text"]})
    except (ValueError, KeyError):
        return web.json_response({"text": raw})


# ---------- агенты (только admin) ----------

@routes.get("/api/agents")
async def agents_list(request: web.Request) -> web.Response:
    require_admin(request)
    items = []
    for a in await ctx_of(request).db.list_agents():
        cfg = a.get("config") or {}
        items.append({
            "name": a["name"], "display_name": a.get("display_name") or a["name"], "role": a.get("role_description") or a["role"],
            "enabled": bool(a["enabled"]), "status": effective_status(a.get("last_seen"), a.get("status")),
            "last_seen": iso_local(a.get("last_seen")), "kind": cfg.get("kind") or "generic", "bot_username": a.get("bot_username"),
            "draft": bool(cfg.get("draft") or cfg.get("awaiting_name") or cfg.get("awaiting_token")),
            "last_error": (a.get("last_error") or "")[:200] or None,
        })
    return web.json_response({"agents": items})


@routes.post("/api/agents/{name}/toggle")
async def agents_toggle(request: web.Request) -> web.Response:
    """Включить/выключить: Кимо присылает владельцу подтверждение «да/нет» в Telegram (так же, как при команде в чате)."""
    require_admin(request)
    ctx, user = ctx_of(request), me(request)
    name = request.match_info["name"]
    if not NAME_RE.match(name) or await ctx.db.get_agent(name) is None:
        raise ApiError(404, "not_found", "Агент не найден")
    enabled = (await body(request)).get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(400, "bad_param", "enabled: true или false")
    action = {"type": "manage_agent", "agent": name, "op": "start" if enabled else "stop"}
    raw = await ask_kimo(ctx, {"type": "user_command", "source": "miniapp", "tg_id": user["tg_id"], "action": action},
                         user["tg_id"], timeout=ctx.kimo_wait)
    await ctx.db.audit(to_db(now_utc()), f"miniapp:{user['tg_id']}", "agent_toggle_request", name, {"enabled": enabled})
    return web.json_response({"text": json.loads(raw).get("text", "") if raw.startswith("{") else raw})


@routes.post("/api/agents")
async def agents_create(request: web.Request) -> web.Response:
    """Создание нового агента через Мимо. Токен из BotFather и права владелец вводит и подтверждает в Telegram."""
    require_admin(request)
    ctx, user = ctx_of(request), me(request)
    description = str((await body(request)).get("description", "")).strip()
    if not 10 <= len(description) <= 3000:
        raise ApiError(400, "bad_param", "Описание агента: от 10 до 3000 символов")
    action = {"type": "create_agent", "description": description}
    raw = await ask_kimo(ctx, {"type": "user_command", "source": "miniapp", "tg_id": user["tg_id"], "action": action},
                         user["tg_id"], timeout=max(ctx.kimo_wait, 90.0))
    await ctx.db.audit(to_db(now_utc()), f"miniapp:{user['tg_id']}", "agent_create_request", None, {"len": len(description)})
    return web.json_response({"text": json.loads(raw).get("text", "") if raw.startswith("{") else raw})


# ---------- доступы (только admin) ----------

@routes.get("/api/access")
async def access_list(request: web.Request) -> web.Response:
    require_admin(request)
    rows = await ctx_of(request).db.grants_list_active(to_db(now_utc()))
    return web.json_response({"grants": [
        {"tg_id": g["tg_id"], "username": g["username"], "name": g["first_name"], "expires_at": iso_local(g["expires_at"]),
         "created_at": iso_local(g["created_at"])} for g in rows]})


@routes.post("/api/access/grant")
async def access_grant(request: web.Request) -> web.Response:
    require_admin(request)
    ctx = ctx_of(request)
    data = await body(request)
    tg_id, days = int_param(data.get("tg_id"), "tg_id", 1), int_param(data.get("days"), "days", 1, grants.MAX_GRANT_DAYS)
    user = await ctx.db.ensure_user(tg_id, to_db(now_utc()))
    until = await grants.grant(ctx.db, user, days, granted_by=me(request)["tg_id"])
    if ctx.notify:
        await ctx.notify(tg_id, f"🎁 Вам открыт доступ к Кимо до {grants.fmt(until)}.")
    return web.json_response({"tg_id": tg_id, "expires_at": until.astimezone(TZ).isoformat()})


@routes.post("/api/access/revoke")
async def access_revoke(request: web.Request) -> web.Response:
    require_admin(request)
    ctx = ctx_of(request)
    tg_id = int_param((await body(request)).get("tg_id"), "tg_id", 1)
    user = await ctx.db.get_user_by_tg(tg_id)
    if user is None or tg_id == ctx.settings.admin_id or not await grants.revoke(ctx.db, user):
        raise ApiError(404, "not_found", "Активного доступа нет")
    if ctx.notify:
        await ctx.notify(tg_id, "⛔ Ваш доступ к Кимо отключён.")
    return web.json_response({"ok": True})


# ---------- Business-чат ----------

@routes.get("/api/business")
async def business_state(request: web.Request) -> web.Response:
    require(request, "business")
    ctx, user = ctx_of(request), me(request)
    owner = user["id"]
    conns = await ctx.db.bconn_list(owner)
    rules = await ctx.db.brules_get(owner) or {}
    chats = []
    for c in await ctx.db.bchat_list(owner, 100):
        chats.append({
            "chat_id": c["chat_id"], "name": c["client_name"], "username": c["username"], "excluded": bool(c["excluded"]),
            "paused": biz.is_paused(c), "paused_until": iso_local(c["paused_until"]), "last_incoming": iso_local(c["last_incoming_at"]),
        })
    return web.json_response({
        "connections": [{"can_reply": bool(c["can_reply"]), "is_enabled": bool(c["is_enabled"]), "ai_enabled": bool(c["ai_enabled"]),
                         "updated": iso_local(c["updated_at"])} for c in conns],
        "rules": {k: rules.get(f, "" if k not in ("pause", "flood") else (6 if k == "pause" else 6))
                  for k, f in biz.RULE_FIELDS.items()},
        "owner_name_default": user.get("first_name") or "",
        "chats": chats,
    })


@routes.put("/api/business/rules")
async def business_rules(request: web.Request) -> web.Response:
    require(request, "business")
    ctx, owner = ctx_of(request), me(request)["id"]
    data = await body(request)
    fields: dict[str, Any] = {}
    for key, value in data.items():
        try:
            column, clean = biz.clean_rule(key, value)
        except biz.RuleError as exc:
            raise ApiError(400, "bad_param", str(exc)) from None
        fields[column] = clean
    if fields:
        await ctx.db.brules_set(owner, to_db(now_utc()), **fields)
    return web.json_response({"ok": True})


@routes.post("/api/business/pause")
async def business_pause(request: web.Request) -> web.Response:
    require(request, "business")
    enabled = (await body(request)).get("ai_enabled")
    if not isinstance(enabled, bool):
        raise ApiError(400, "bad_param", "ai_enabled: true или false")
    await ctx_of(request).db.bconn_set_ai(me(request)["id"], enabled)
    return web.json_response({"ok": True})


@routes.post("/api/business/chats/{chat_id}/pause")
async def business_chat_pause(request: web.Request) -> web.Response:
    require(request, "business")
    excluded = (await body(request)).get("excluded")
    if not isinstance(excluded, bool):
        raise ApiError(400, "bad_param", "excluded: true или false")
    fields: dict[str, Any] = {"excluded": int(excluded)}
    if not excluded:
        fields["paused_until"] = None
    ok = await ctx_of(request).db.bchat_set(me(request)["id"], int_param(request.match_info["chat_id"], "chat_id"), **fields)
    if not ok:
        raise ApiError(404, "not_found", "Чат не найден")
    return web.json_response({"ok": True})


@routes.get("/api/business/log")
async def business_log(request: web.Request) -> web.Response:
    require(request, "business")
    chat_id = int_param(request.query.get("chat_id"), "chat_id")
    rows = await ctx_of(request).db.bmsg_recent(me(request)["id"], chat_id, int_param(request.query.get("limit", 50), "limit", 1, 100))
    return web.json_response({"messages": [{"role": r["role"], "text": r["text"], "at": iso_local(r["created_at"])} for r in rows]})


# ---------- рассылки ----------

def sched_json(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": s["id"], "chat_id": s["chat_id"], "time": s["send_time"], "days": s["weekdays"], "tz": s["tz"], "mode": s["mode"],
            "text": s["text"], "topic": s["topic"], "channel": s["channel"], "active": bool(s["active"]),
            "last_run": iso_local(s["last_run"]), "last_status": s["last_status"]}


@routes.get("/api/broadcasts")
async def broadcasts_state(request: web.Request) -> web.Response:
    require(request, "broadcasts")
    ctx, owner = ctx_of(request), me(request)["id"]
    log_rows = await ctx.db.blog_list(owner, 30)
    link = f"https://t.me/{ctx.bot_username}?start=sb_{me(request)['tg_id']}" if ctx.bot_username else None
    return web.json_response({
        "schedules": [sched_json(s) for s in await ctx.db.sched_list(owner)],
        "chats": [{**c, "last_incoming_at": iso_local(c["last_incoming_at"])} for c in await bcast.known_chats(ctx.db, owner)],
        "log": [{"schedule_id": r["schedule_id"], "chat_id": r["chat_id"], "at": iso_local(r["ts"]), "status": r["status"],
                 "detail": r["detail"]} for r in log_rows],
        "link": link,
    })


@routes.post("/api/broadcasts")
async def broadcasts_create(request: web.Request) -> web.Response:
    require(request, "broadcasts")
    ctx, user = ctx_of(request), me(request)
    data = await body(request)
    try:
        sid = await bcast.create_schedule(
            ctx.db, user, int_param(data.get("chat_id"), "chat_id"), str(data.get("time", "")),
            bcast.parse_days(str(data.get("days", "daily"))), str(data.get("mode", "text")),
            str(data.get("text", "")), str(data.get("topic", "")), data.get("tz") or None, data.get("channel") or None)
    except bcast.BroadcastError as exc:
        raise ApiError(400, "bad_param", str(exc)) from None
    return web.json_response({"id": sid}, status=201)


@routes.put("/api/broadcasts/{id}")
async def broadcasts_update(request: web.Request) -> web.Response:
    require(request, "broadcasts")
    ctx, owner = ctx_of(request), me(request)["id"]
    sid = int_param(request.match_info["id"], "id", 1)
    sch = await ctx.db.sched_get(owner, sid)
    if sch is None:
        raise ApiError(404, "not_found", "Рассылка не найдена")
    data = await body(request)
    fields: dict[str, Any] = {}
    try:
        if "time" in data:
            fields["send_time"] = bcast.parse_time(str(data["time"]))
        if "days" in data:
            fields["weekdays"] = bcast.parse_days(str(data["days"]))
        if "tz" in data:
            fields["tz"] = bcast.check_tz(str(data["tz"]))
        if "mode" in data:
            if data["mode"] not in ("text", "ai"):
                raise bcast.BroadcastError("mode: text или ai")
            fields["mode"] = data["mode"]
        for key in ("text", "topic"):
            if key in data:
                limit = bcast.MAX_TEXT if key == "text" else bcast.MAX_TOPIC
                if len(str(data[key])) > limit:
                    raise bcast.BroadcastError("Слишком длинный текст")
                fields[key] = str(data[key]).strip()
        if "active" in data:
            if not isinstance(data["active"], bool):
                raise bcast.BroadcastError("active: true или false")
            if data["active"] and await ctx.db.optout_has(owner, sch["chat_id"]):
                raise bcast.BroadcastError("Человек написал «стоп»: включить нельзя")
            fields["active"] = int(data["active"])
    except bcast.BroadcastError as exc:
        raise ApiError(400, "bad_param", str(exc)) from None
    merged = {**sch, **fields}
    if (merged["mode"] == "text" and not merged["text"]) or (merged["mode"] == "ai" and not merged["topic"]):
        raise ApiError(400, "bad_param", "Для выбранного режима нужен текст или тема")
    if fields:
        fields["last_run_date"] = None if ("send_time" in fields or "weekdays" in fields) else sch["last_run_date"]
        await ctx.db.sched_update(owner, sid, **fields)
    return web.json_response({"ok": True})


@routes.delete("/api/broadcasts/{id}")
async def broadcasts_delete(request: web.Request) -> web.Response:
    require(request, "broadcasts")
    if not await ctx_of(request).db.sched_delete(me(request)["id"], int_param(request.match_info["id"], "id", 1)):
        raise ApiError(404, "not_found", "Рассылка не найдена")
    return web.json_response({"ok": True})


# ---------- статика Mini App ----------

async def index(request: web.Request) -> web.StreamResponse:
    webapp = ctx_of(request).webapp_dir
    page = webapp / "index.html" if webapp else None
    if page is None or not page.is_file():
        raise web.HTTPNotFound(text="Mini App ещё не собрано")
    return web.FileResponse(page, headers={"Cache-Control": "no-cache"})


def create_app(ctx: ApiContext) -> web.Application:
    app = web.Application(middlewares=[headers_middleware, error_middleware, auth_middleware], client_max_size=MAX_BODY)
    app["ctx"] = ctx
    app.add_routes(routes)
    app.router.add_get("/", index)
    if ctx.webapp_dir and ctx.webapp_dir.is_dir():
        app.router.add_static("/static", ctx.webapp_dir / "static", follow_symlinks=False) if (ctx.webapp_dir / "static").is_dir() else None
    return app
