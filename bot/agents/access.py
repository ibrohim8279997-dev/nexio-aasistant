"""Права агентов на таблицы. Проверка в общем слое доступа к БД, а не только в промптах.

Агент получает ScopedDB вместо Database: любой метод, трогающий таблицу вне allowed_tables, падает с AccessDenied.
Методы без описания в METHOD_TABLES запрещены (в том числе прямой доступ к соединению).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bot.db import Database

# Таблицы, доступ к которым жёстко ограничен независимо от конфига агента
RESTRICTED_TABLES: dict[str, set[str]] = {
    "expenses": {"kimo"},
    "payments": {"kimo"},
    "secrets": {"kimo"},
}

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
        # secrets
        "set_secret": ["secrets"], "get_secret": ["secrets"], "secret_names": ["secrets"],
        "delete_secret": ["secrets"],
        # rates_cache
        "save_rate": ["rates_cache"], "last_rate": ["rates_cache"],
    }.items()
}


class AccessDenied(PermissionError):
    """Агенту запрещён доступ к таблице."""


def can_access(agent: str, allowed_tables: Iterable[str], table: str) -> bool:
    if table.startswith(f"agent_{agent}_"):
        return True  # собственные таблицы агента
    if table in RESTRICTED_TABLES and agent not in RESTRICTED_TABLES[table]:
        return False
    return table in set(allowed_tables)


class ScopedDB:
    def __init__(self, db: Database, agent: str, allowed_tables: Iterable[str]) -> None:
        self._db = db
        self._agent = agent
        self._allowed = frozenset(allowed_tables)

    @property
    def agent(self) -> str:
        return self._agent

    def allows(self, table: str) -> bool:
        return can_access(self._agent, self._allowed, table)

    def __getattr__(self, name: str) -> Any:
        tables = METHOD_TABLES.get(name)
        if tables is None:
            raise AccessDenied(f"Агенту {self._agent} недоступен метод БД {name}")
        denied = sorted(t for t in tables if not self.allows(t))
        if denied:
            raise AccessDenied(f"Агенту {self._agent} запрещён доступ к таблицам: {', '.join(denied)}")
        return getattr(self._db, name)
