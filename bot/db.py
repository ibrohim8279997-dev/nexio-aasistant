"""SQLite (WAL): схема, миграции, доступ к данным.

Все запросы параметризованные. Все данные пользователя выбираются только по его user_id.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

# Каждая миграция — SQL-скрипт. Номер версии = индекс + 1 (хранится в PRAGMA user_version).
# Новые изменения схемы добавляйте в конец списка, старые не меняйте.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE users (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id        INTEGER NOT NULL UNIQUE,
        username     TEXT,
        first_name   TEXT,
        created_at   TEXT NOT NULL,
        sub_until    TEXT,
        sub_notified INTEGER NOT NULL DEFAULT 0,
        lang         TEXT NOT NULL DEFAULT 'ru'
    );
    CREATE INDEX idx_users_sub_until ON users(sub_until);

    CREATE TABLE tasks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        text       TEXT NOT NULL,
        due_at     TEXT,
        has_time   INTEGER NOT NULL DEFAULT 0,
        reminded   INTEGER NOT NULL DEFAULT 0,
        done_at    TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_tasks_user_due ON tasks(user_id, due_at);
    CREATE INDEX idx_tasks_user_done ON tasks(user_id, done_at);
    CREATE INDEX idx_tasks_reminders ON tasks(reminded, done_at, due_at);

    CREATE TABLE expenses (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        amount     REAL NOT NULL CHECK (amount > 0),
        currency   TEXT NOT NULL CHECK (currency IN ('UZS', 'USD')),
        category   TEXT NOT NULL,
        note       TEXT,
        rate_used  REAL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_expenses_user_created ON expenses(user_id, created_at);

    CREATE TABLE payments (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        charge_id  TEXT NOT NULL UNIQUE,
        amount_xtr INTEGER NOT NULL DEFAULT 0,
        provider   TEXT NOT NULL DEFAULT 'stars',
        amount_uzs INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_payments_user_created ON payments(user_id, created_at);

    CREATE TABLE secrets (
        name            TEXT PRIMARY KEY,
        encrypted_value TEXT NOT NULL
    );

    CREATE TABLE rates_cache (
        date       TEXT PRIMARY KEY,
        usd_uzs    REAL NOT NULL,
        fetched_at TEXT NOT NULL
    );
    """,
]


def _restrict_permissions(path: Path) -> None:
    """chmod 600 для файла БД и WAL-файлов (если ФС поддерживает)."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{path}{suffix}")
        if p.exists():
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass


class Database:
    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path

    @classmethod
    async def connect(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        db = cls(conn, path)
        _restrict_permissions(path)
        return db

    async def close(self) -> None:
        await self.conn.close()

    # ---------- миграции ----------

    async def schema_version(self) -> int:
        row = await self._one("PRAGMA user_version")
        return int(row["user_version"]) if row else 0

    async def migrate(self) -> None:
        version = await self.schema_version()
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            try:
                await self.conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
            except Exception:
                await self.conn.rollback()
                log.error("Миграция %s не применилась", number)
                raise
            log.info("Применена миграция БД №%s", number)
        _restrict_permissions(self.path)

    # ---------- помощники ----------

    async def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur  # type: ignore[return-value]

    # ---------- пользователи ----------

    async def get_or_create_user(self, tg_id: int, username: str | None, first_name: str | None, now: str) -> dict[str, Any]:
        user = await self.get_user_by_tg(tg_id)
        if user and user["username"] == username and user["first_name"] == first_name:
            return user
        await self._write(
            """INSERT INTO users (tg_id, username, first_name, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name""",
            (tg_id, username, first_name, now),
        )
        user = await self.get_user_by_tg(tg_id)
        assert user is not None
        return user

    async def get_user_by_tg(self, tg_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    async def set_sub_until(self, user_id: int, sub_until: str | None) -> None:
        await self._write("UPDATE users SET sub_until = ?, sub_notified = 0 WHERE id = ?", (sub_until, user_id))

    async def mark_sub_notified(self, user_id: int) -> None:
        await self._write("UPDATE users SET sub_notified = 1 WHERE id = ?", (user_id,))

    async def users_to_remind_sub(self, now: str, limit_until: str) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM users WHERE sub_notified = 0 AND sub_until > ? AND sub_until <= ?",
            (now, limit_until),
        )

    async def list_users(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))

    async def count_users(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    async def count_active_subs(self, now: str) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM users WHERE sub_until > ?", (now,))
        return int(row["n"]) if row else 0

    # ---------- задачи ----------

    async def add_task(self, user_id: int, text: str, due_at: str | None, has_time: bool, now: str) -> int:
        cur = await self._write(
            "INSERT INTO tasks (user_id, text, due_at, has_time, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, text, due_at, int(has_time), now),
        )
        return int(cur.lastrowid)

    async def get_task(self, user_id: int, task_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))

    async def tasks_between(self, user_id: int, start: str, end: str, overdue_before: str | None = None) -> list[dict[str, Any]]:
        """Задачи со сроком в [start, end). Если задан overdue_before — плюс невыполненные просроченные."""
        if overdue_before:
            return await self._all(
                """SELECT * FROM tasks WHERE user_id = ?
                   AND ((due_at >= ? AND due_at < ?) OR (done_at IS NULL AND due_at < ?))
                   ORDER BY done_at IS NOT NULL, due_at""",
                (user_id, start, end, overdue_before),
            )
        return await self._all(
            "SELECT * FROM tasks WHERE user_id = ? AND due_at >= ? AND due_at < ? ORDER BY done_at IS NOT NULL, due_at",
            (user_id, start, end),
        )

    async def open_tasks(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM tasks WHERE user_id = ? AND done_at IS NULL ORDER BY due_at IS NULL, due_at LIMIT ?",
            (user_id, limit),
        )

    async def complete_task(self, user_id: int, task_id: int, now: str) -> bool:
        cur = await self._write(
            "UPDATE tasks SET done_at = ? WHERE id = ? AND user_id = ? AND done_at IS NULL", (now, task_id, user_id)
        )
        return cur.rowcount > 0

    async def reschedule_task(self, user_id: int, task_id: int, due_at: str, has_time: bool) -> bool:
        cur = await self._write(
            "UPDATE tasks SET due_at = ?, has_time = ?, reminded = 0, done_at = NULL WHERE id = ? AND user_id = ?",
            (due_at, int(has_time), task_id, user_id),
        )
        return cur.rowcount > 0

    async def delete_task(self, user_id: int, task_id: int) -> bool:
        cur = await self._write("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        return cur.rowcount > 0

    async def due_reminders(self, now: str, not_before: str) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT t.id, t.user_id, t.text, t.due_at, u.tg_id, u.sub_until
               FROM tasks t JOIN users u ON u.id = t.user_id
               WHERE t.reminded = 0 AND t.done_at IS NULL AND t.has_time = 1
                 AND t.due_at <= ? AND t.due_at >= ?
               ORDER BY t.due_at LIMIT 200""",
            (now, not_before),
        )

    async def mark_reminded(self, task_id: int) -> None:
        await self._write("UPDATE tasks SET reminded = 1 WHERE id = ?", (task_id,))

    async def skip_stale_reminders(self, not_before: str) -> None:
        """Слишком старые напоминания (бот был выключен) не отправляем."""
        await self._write(
            "UPDATE tasks SET reminded = 1 WHERE reminded = 0 AND done_at IS NULL AND has_time = 1 AND due_at < ?",
            (not_before,),
        )

    async def task_rows_due_between(self, user_id: int, start: str, end: str) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT due_at, done_at FROM tasks WHERE user_id = ? AND due_at >= ? AND due_at < ?",
            (user_id, start, end),
        )

    async def done_times_since(self, user_id: int, since: str) -> list[str]:
        rows = await self._all(
            "SELECT done_at FROM tasks WHERE user_id = ? AND done_at IS NOT NULL AND done_at >= ?", (user_id, since)
        )
        return [r["done_at"] for r in rows]

    async def count_tasks(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM tasks")
        return int(row["n"]) if row else 0

    # ---------- расходы ----------

    async def add_expense(
        self,
        user_id: int,
        amount: float,
        currency: str,
        category: str,
        note: str | None,
        rate_used: float | None,
        now: str,
    ) -> int:
        cur = await self._write(
            """INSERT INTO expenses (user_id, amount, currency, category, note, rate_used, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, amount, currency, category, note, rate_used, now),
        )
        return int(cur.lastrowid)

    async def expenses_between(self, user_id: int, start: str, end: str) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM expenses WHERE user_id = ? AND created_at >= ? AND created_at < ? ORDER BY created_at",
            (user_id, start, end),
        )

    async def last_expense(self, user_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))

    async def delete_expense(self, user_id: int, expense_id: int) -> bool:
        cur = await self._write("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
        return cur.rowcount > 0

    async def count_expenses(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM expenses")
        return int(row["n"]) if row else 0

    # ---------- платежи ----------

    async def add_payment(
        self,
        user_id: int,
        charge_id: str,
        amount_xtr: int,
        now: str,
        provider: str = "stars",
        amount_uzs: int | None = None,
    ) -> bool:
        """False, если платёж с таким charge_id уже записан (повторный апдейт)."""
        try:
            await self._write(
                """INSERT INTO payments (user_id, charge_id, amount_xtr, provider, amount_uzs, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, charge_id, amount_xtr, provider, amount_uzs, now),
            )
        except sqlite3.IntegrityError:
            await self.conn.rollback()
            return False
        return True

    async def payments_summary(self) -> dict[str, int]:
        row = await self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_xtr), 0) AS xtr, COALESCE(SUM(amount_uzs), 0) AS uzs FROM payments"
        )
        return {k: int(v) for k, v in (row or {"n": 0, "xtr": 0, "uzs": 0}).items()}

    # ---------- секреты (значения уже зашифрованы) ----------

    async def set_secret(self, name: str, encrypted_value: str) -> None:
        await self._write(
            "INSERT INTO secrets (name, encrypted_value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET encrypted_value = excluded.encrypted_value",
            (name, encrypted_value),
        )

    async def get_secret(self, name: str) -> str | None:
        row = await self._one("SELECT encrypted_value FROM secrets WHERE name = ?", (name,))
        return row["encrypted_value"] if row else None

    async def secret_names(self) -> list[str]:
        return [r["name"] for r in await self._all("SELECT name FROM secrets ORDER BY name")]

    async def delete_secret(self, name: str) -> bool:
        cur = await self._write("DELETE FROM secrets WHERE name = ?", (name,))
        return cur.rowcount > 0

    # ---------- курс валют ----------

    async def save_rate(self, day: str, usd_uzs: float, fetched_at: str) -> None:
        await self._write(
            "INSERT INTO rates_cache (date, usd_uzs, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET usd_uzs = excluded.usd_uzs, fetched_at = excluded.fetched_at",
            (day, usd_uzs, fetched_at),
        )

    async def last_rate(self) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM rates_cache ORDER BY fetched_at DESC LIMIT 1")
