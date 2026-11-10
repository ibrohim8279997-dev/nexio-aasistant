"""Отчёты по агентам для чата: короткие, читаемые, без SQL-таблиц и без секретов. Данные берутся только через AdminOps."""
from __future__ import annotations

from html import escape
from typing import Any

from bot.agents.admin_ops import AdminOps, TableInfo
from bot.agents.health import STATUS_ICON, fmt_ago, fmt_clock, reason_text
from bot.logging_setup import redact_text
from bot.timeutil import now_utc

MAX_MESSAGE = 3500
PROBLEM = {"offline", "error"}


def _cut(text: str, limit: int = MAX_MESSAGE) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n… (обрезано)"


def _pre(lines: list[str]) -> str:
    return "<pre>" + escape("\n".join(redact_text(ln)[:220] for ln in lines)) + "</pre>"


def _queue_line(counts: dict[str, int]) -> str:
    return " · ".join(f"{k} {counts.get(k, 0)}" for k in ("new", "taken", "done", "failed"))


async def agents_report(ops: AdminOps, include_logs: bool = True) -> str:
    """Кто работает: статусы по display_name, для проблемных последние 10 строк лога и ошибка, очередь по статусам."""
    agents = await ops.list_agents()
    queue = await ops.queue_overview()
    now = now_utc()
    names = {a["name"]: a["display_name"] for a in agents}
    online = sum(1 for a in agents if a["status"] in ("online", "busy"))
    lines = [f"📋 <b>Агенты</b>: работают {online} из {len(agents)}", ""]
    problems = []
    for a in agents:
        icon = STATUS_ICON.get(a["status"], "⚫")
        seen = f"{fmt_clock(a['last_seen'])} ({fmt_ago(a['age'])})" if a["last_seen"] else "не выходил на связь"
        extra = ""
        if a["status"] == "busy" and (a["detail"] or {}).get("reason") == "rate_limit":
            extra = f" — лимит Groq, повтор ~{(a['detail'] or {}).get('until_clock', '—')}"
        if not a["enabled"]:
            extra += " (выключен)"
        lines.append(f"{icon} <b>{escape(a['display_name'])}</b> — {a['status']}, last_seen {seen}{escape(extra)}")
        lines.append(f"   очередь: {_queue_line(queue['counts'].get(a['name'], {}))}")
        if a["status"] in PROBLEM and a["enabled"]:
            problems.append(a)
    for a in problems:
        lines += ["", f"⚠️ <b>{escape(a['display_name'])}</b>: {escape(reason_text(a['display_name'], a['last_seen'], 'offline' if a['status'] == 'offline' else a['status'], a['last_error'], a['detail'], now))}"]
        if a["last_error"]:
            lines.append(f"Последняя ошибка: {escape(redact_text(a['last_error']))}")
        if include_logs:
            tail = await ops.read_log(a["name"], None, 10)
            lines.append("Лог (последние 10 записей):" if tail else "Лог пуст или не найден.")
            if tail:
                lines.append(_pre(tail))
    if queue["failed"]:
        lines += ["", "<b>Последние неудачные задания</b>"]
        for f in queue["failed"]:
            lines.append(f"#{f['id']} → {escape(names.get(f['to'], f['to']))}: {escape(f['result'][:160])}")
    return _cut("\n".join(lines))


async def agent_logs(ops: AdminOps, display: str, name: str, minutes: int | None, lines: int = 30) -> str:
    rows = await ops.read_log(name, minutes, lines)
    title = f"📜 <b>Лог {escape(display)}</b>" + (f" за {minutes} мин" if minutes else " (последние записи)")
    if not rows:
        return f"{title}\nЗаписей нет."
    return _cut(f"{title}\n{_pre(rows)}")


def tables_report(display: str, tables: list[TableInfo]) -> str:
    if not tables:
        return f"🗂 У {escape(display)} нет доступных таблиц."
    lines = [f"🗂 <b>Таблицы {escape(display)}</b>"]
    for t in tables:
        mark = " (значения скрыты)" if t.rows_hidden else ""
        lines.append(f"• <code>{escape(t.name)}</code>{mark}: {escape(', '.join(t.columns))}")
    return _cut("\n".join(lines))


def rows_report(display: str, table: str, rows: list[dict[str, Any]]) -> str:
    title = f"🗂 <b>{escape(display)} · {escape(table)}</b> (последние {len(rows)})"
    if not rows:
        return f"{title}\nСтрок нет."
    body = [", ".join(f"{k}={v}" for k, v in row.items()) for row in rows]
    return _cut(f"{title}\n{_pre(body)}")
