"""Права агентов на таблицы. Проверка в общем слое доступа к БД, а не только в промптах.

Агент получает ScopedDB вместо Database: любой метод, трогающий таблицу вне allowed_tables, падает с AccessDenied.
Методы без описания в METHOD_TABLES запрещены (в том числе прямой доступ к соединению).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bot.db import Database

# Таблицы, доступ к которым ограничен независимо от списка allowed_tables в конфиге агента.
# Расходы и платежи видит только роль admin (роль хранится в таблице agents, а не в коде).
RESTRICTED_TABLES: frozenset[str] = frozenset({"expenses", "payments"})
# Значения секретов не видит никто через слой данных: ни агент, ни admin (имена — только через AdminOps).
HARD_DENIED_TABLES: frozenset[str] = frozenset({"secrets"})

# Метод Database -> таблицы, которые он читает или пишет
METHOD_TABLES: dict[str, frozenset[str]] = {
    name: frozenset(tables)
    for name, tables in {
        # users
        "get_or_create_user": ["users"], "get_user_by_tg": ["users"], "get_user": ["users"],
        "ensure_user": ["users"], "set_sub_until": ["users"], "mark_sub_notified": ["users"],
        "users_to_remind_sub": ["users"], "list_users": ["users"], "count_users": ["users"],
        "count_active_subs": ["users"],
        # tasks
        "add_task": ["tasks"], "get_task": ["tasks"], "tasks_between": ["tasks"], "open_tasks": ["tasks"],
        "complete_task": ["tasks"], "reschedule_task": ["tasks"], "delete_task": ["tasks"],
        "due_reminders": ["tasks", "users"], "mark_reminded": ["tasks"], "skip_stale_reminders": ["tasks"],
        "task_rows_due_between": ["tasks"], "done_times_since": ["tasks"], "count_tasks": ["tasks"],
        "upcoming_reminders": ["tasks"],
        # expenses
        "add_expense": ["expenses"], "expenses_between": ["expenses"], "last_expense": ["expenses"],
        "delete_expense": ["expenses"], "count_expenses": ["expenses"],
        # payments
        "add_payment": ["payments"], "payments_summary": ["payments"],
        # rates_cache
        "save_rate": ["rates_cache"], "last_rate": ["rates_cache"],
        # собственная таблица ассистента
        "add_note": ["agent_assistant_notes"], "list_notes": ["agent_assistant_notes"],
        "delete_note": ["agent_assistant_notes"],
    }.items()
}


class AccessDenied(PermissionError):
    """Агенту запрещён доступ к таблице."""


def can_access(agent: str, allowed_tables: Iterable[str], table: str, role: str = "agent") -> bool:
    if table in HARD_DENIED_TABLES:
        return False
    if table.startswith(f"agent_{agent}_"):
        return True  # собственные таблицы агента
    if table in RESTRICTED_TABLES and role != "admin":
        return False
    return table in set(allowed_tables)


class ScopedDB:
    """Доступ агента к данным. live=True: роль и список таблиц читаются из таблицы agents при каждом запросе,
    поэтому отзыв прав действует сразу, без перезапуска процесса."""

    def __init__(self, db: Database, agent: str, allowed_tables: Iterable[str], role: str = "agent",
                 live: bool = False) -> None:
        self._db = db
        self._agent = agent
        self._allowed = frozenset(allowed_tables)
        self._role = role
        self._live = live

    @property
    def agent(self) -> str:
        return self._agent

    async def current(self) -> tuple[str, frozenset[str]]:
        if not self._live:
            return self._role, self._allowed
        row = await self._db.get_agent(self._agent)
        if row is None:
            return "agent", frozenset()  # агента нет в реестре — прав нет
        return row.get("access_role") or "agent", frozenset(row.get("allowed_tables") or [])

    async def allows(self, table: str) -> bool:
        role, allowed = await self.current()
        return can_access(self._agent, allowed, table, role)

    def __getattr__(self, name: str) -> Any:
        tables = METHOD_TABLES.get(name)
        if tables is None:
            raise AccessDenied(f"Агенту {self._agent} недоступен метод БД {name}")
        method = getattr(self._db, name)

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            role, allowed = await self.current()
            denied = sorted(t for t in tables if not can_access(self._agent, allowed, t, role))
            if denied:
                raise AccessDenied(f"Агенту {self._agent} запрещён доступ к таблицам: {', '.join(denied)}")
            return await method(*args, **kwargs)

        return guarded
