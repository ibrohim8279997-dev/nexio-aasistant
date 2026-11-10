"""Административный слой доступа: чтение структуры, данных, логов и статусов агентов.

Права берутся из таблицы agents при каждом вызове (поле access_role), а не из кода:
- admin   (Кимо)  — все агенты, структура и данные всех таблиц, кроме значений секретов;
- creator (Мимо)  — только агенты с created_by = он сам; расходы, платежи, секреты и чужие агенты недоступны;
- agent           — ничего (только собственные таблицы через ScopedDB).
Значения секретов и токены не видит никто: максимум — имена секретов. Каждое обращение пишется в audit_log
(в том числе отказы). Запись и опасные действия идут отдельно, через подтверждение владельца (confirm.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bot.agents.access import HARD_DENIED_TABLES, RESTRICTED_TABLES
from bot.agents.health import effective_status, seconds_since
from bot.agents.registry import NAME_RE, AgentConfig
from bot.config import BASE_DIR
from bot.db import Database
from bot.logging_setup import redact_text
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

MAX_ROWS = 50
MAX_LOG_LINES = 200
SYSTEM_TABLES = frozenset({"users", "agents", "audit_log", "pending_actions", "tasks_queue", "secrets"})
CREATOR_GRANTABLE = frozenset({"tasks", "rates_cache"})  # единственные общие таблицы, видимые creator
_SENSITIVE_COLUMN = re.compile(r"token|secret|password|passwd|api_?key|encrypted", re.IGNORECASE)
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


class AdminError(PermissionError):
    """Действие запрещено правами роли. Текст можно показывать владельцу."""


@dataclass
class TableInfo:
    name: str
    columns: list[str]
    owner: str | None  # агент-владелец таблицы (agent_<имя>_*) или None для общих
    rows_hidden: bool = False  # значения скрыты (secrets)


def mask_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if _SENSITIVE_COLUMN.search(key):
            out[key] = "***"
        elif isinstance(value, str):
            out[key] = redact_text(value)
        else:
            out[key] = value
    return out


class AdminOps:
    def __init__(self, db: Database, actor: str, log_dir: Path | None = None) -> None:
        self._db = db
        self.actor = actor
        self.log_dir = log_dir or BASE_DIR / "logs"

    # ---------- права ----------

    async def actor_config(self) -> AgentConfig:
        row = await self._db.get_agent(self.actor)
        if row is None:
            raise AdminError("Агент не найден в реестре")
        return AgentConfig.from_row(row)

    async def _audit(self, action: str, target: str | None, details: dict[str, Any], result: str = "ok") -> None:
        safe = {k: (redact_text(v) if isinstance(v, str) else v) for k, v in details.items()}
        try:
            await self._db.audit(to_db(now_utc()), self.actor, action, target, safe, result)
        except Exception:  # noqa: BLE001 — сбой аудита не должен раскрывать данные, но и не маскируется молча
            log.exception("audit_log: не удалось записать %s", action)
            raise

    async def scope(self, target: str, action: str = "access") -> AgentConfig:
        """Агент-цель, если роль актора позволяет с ним работать. Иначе AdminError (и запись в аудит)."""
        actor = await self.actor_config()
        row = await self._db.get_agent(target) if NAME_RE.match(target or "") else None
        if row is None:
            raise AdminError("Нет такого агента")
        cfg = AgentConfig.from_row(row)
        allowed = actor.access_role == "admin" or (
            actor.access_role == "creator" and cfg.created_by == actor.name and cfg.name != actor.name
        )
        if not allowed:
            await self._audit(action, target, {"role": actor.access_role}, "denied")
            raise AdminError(f"Нет прав: {actor.display_name} не управляет агентом {cfg.display_name}")
        return cfg

    async def require_admin(self, action: str) -> AgentConfig:
        actor = await self.actor_config()
        if actor.access_role != "admin":
            await self._audit(action, None, {"role": actor.access_role}, "denied")
            raise AdminError("Это действие доступно только роли admin")
        return actor

    async def visible_agents(self) -> list[AgentConfig]:
        actor = await self.actor_config()
        agents = [AgentConfig.from_row(r) for r in await self._db.list_agents()]
        if actor.access_role == "admin":
            return agents
        if actor.access_role == "creator":
            return [a for a in agents if a.created_by == actor.name and a.name != actor.name]
        await self._audit("list_agents", None, {"role": actor.access_role}, "denied")
        raise AdminError("Нет прав на просмотр агентов")

    # ---------- статусы и очередь ----------

    async def list_agents(self) -> list[dict[str, Any]]:
        agents = await self.visible_agents()
        now = now_utc()
        await self._audit("list_agents", None, {"count": len(agents)})
        return [
            {
                "name": a.name, "display_name": a.display_name, "role": a.role_description,
                "status": effective_status(a.last_seen, a.status, now), "last_seen": a.last_seen,
                "age": seconds_since(a.last_seen, now), "last_error": a.last_error, "detail": a.status_detail,
                "enabled": a.enabled, "created_by": a.created_by, "access_role": a.access_role,
            }
            for a in agents
        ]

    async def queue_overview(self, target: str | None = None) -> dict[str, Any]:
        """Число заданий по статусам для каждого агента и последние 5 failed с причиной."""
        names = [target] if target else None
        if target:
            await self.scope(target, "queue_overview")
            visible = {target}
        else:
            visible = {a.name for a in await self.visible_agents()}
        counts: dict[str, dict[str, int]] = {n: {"new": 0, "taken": 0, "done": 0, "failed": 0} for n in sorted(visible)}
        for row in await self._db.queue_counts():
            if row["to_agent"] in counts:
                counts[row["to_agent"]][row["status"]] = row["n"]
        failed = [
            {"id": t["id"], "to": t["to_agent"], "from": t["from_agent"], "result": redact_text(t["result"] or ""),
             "at": t["updated_at"]}
            for t in await self._db.queue_recent_failed(50)
            if t["to_agent"] in visible
        ][:5]
        await self._audit("queue_overview", ",".join(names) if names else None, {"agents": len(visible)})
        return {"counts": counts, "failed": failed}

    # ---------- структура и данные таблиц ----------

    async def _visible_tables(self, actor: AgentConfig, target: AgentConfig | None) -> list[TableInfo]:
        all_tables = await self._db.table_names()
        if target is None:  # только admin: вся структура БД
            names = list(all_tables)
        else:
            names = [t for t in all_tables if t.startswith(f"agent_{target.name}_")]
            names += [t for t in target.allowed_tables if t in all_tables and t not in names]
        if actor.access_role != "admin":
            names = [
                t for t in names
                if t.startswith("agent_") or (t in CREATOR_GRANTABLE and t not in RESTRICTED_TABLES)
            ]
        agent_names = [a["name"] for a in await self._db.list_agents()]
        result = []
        for name in names:
            columns = [c["name"] for c in await self._db.table_columns(name)]
            owner = next((a for a in agent_names if name.startswith(f"agent_{a}_")), None)
            result.append(TableInfo(name, columns, owner, rows_hidden=name in HARD_DENIED_TABLES))
        return result

    async def list_tables(self, target: str | None = None) -> list[TableInfo]:
        """Структура: таблицы и колонки. admin — любая БД, creator — таблицы своих агентов."""
        actor = await self.actor_config()
        if target is None:
            await self.require_admin("list_tables")
            tables = await self._visible_tables(actor, None)
        else:
            tables = await self._visible_tables(actor, await self.scope(target, "list_tables"))
        await self._audit("list_tables", target, {"tables": [t.name for t in tables]})
        return tables

    async def read_rows(self, target: str, table: str, limit: int = 20) -> list[dict[str, Any]]:
        """Данные таблицы агента (последние строки). Значения секретов и колонки token/secret скрыты."""
        actor = await self.actor_config()
        cfg = await self.scope(target, "read_rows")
        allowed = {t.name: t for t in await self._visible_tables(actor, cfg)}
        if actor.access_role == "admin" and table not in allowed and table in await self._db.table_names():
            allowed[table] = next(t for t in await self._visible_tables(actor, None) if t.name == table)
        info = allowed.get(table)
        if info is None:
            await self._audit("read_rows", target, {"table": table}, "denied")
            raise AdminError(f"Нет доступа к таблице {table}")
        if info.rows_hidden:
            await self._audit("read_rows", target, {"table": table}, "denied")
            raise AdminError("Значения секретов не показываются никому, доступны только имена")
        rows = await self._db.table_rows(table, info.columns, max(1, min(limit, MAX_ROWS)))
        await self._audit("read_rows", target, {"table": table, "rows": len(rows)})
        return [mask_row(r) for r in rows]

    async def secret_names(self) -> list[str]:
        """Только имена секретов, значения недоступны."""
        await self.require_admin("secret_names")
        names = [r["name"] for r in await self._db._all("SELECT name FROM secrets ORDER BY name")]
        await self._audit("secret_names", None, {"count": len(names)})
        return names

    # ---------- логи ----------

    async def read_log(self, target: str, minutes: int | None = None, lines: int = 50) -> list[str]:
        cfg = await self.scope(target, "read_log")
        path = self.log_dir / f"{cfg.name}.log"
        lines = max(1, min(lines, MAX_LOG_LINES))
        result: list[str] = []
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
            except OSError:
                raw = []
            cutoff = datetime.now() - timedelta(minutes=minutes) if minutes else None
            keep = cutoff is None
            for line in raw:
                m = _LOG_TS.match(line)
                if m and cutoff is not None:
                    keep = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= cutoff
                if keep:
                    result.append(redact_text(line)[:400])
            result = result[-lines:]
        await self._audit("read_log", cfg.name, {"minutes": minutes, "lines": len(result)})
        return result

    async def last_error_lines(self, target: str, count: int = 10) -> list[str]:
        """Последние строки лога с ошибками (ERROR/CRITICAL/Traceback) — для диагностики."""
        lines = await self.read_log(target, None, MAX_LOG_LINES)
        marks = ("ERROR", "CRITICAL", "Traceback", "Error:", "Exception")
        bad = [ln for ln in lines if any(m in ln for m in marks)]
        return bad[-count:]

    async def read_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        await self.require_admin("read_audit")
        rows = await self._db.audit_list(limit=max(1, min(limit, 100)))
        return rows
