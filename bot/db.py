"""SQLite (WAL): схема, миграции, доступ к данным.

Все запросы параметризованные. Все данные пользователя выбираются только по его user_id.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sqlite3
from datetime import datetime
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
    # 2: мультиагентность — реестр агентов и шина заданий
    """
    CREATE TABLE IF NOT EXISTS agents (
        name             TEXT PRIMARY KEY,
        role             TEXT NOT NULL,
        bot_username     TEXT,
        system_prompt    TEXT NOT NULL DEFAULT '',
        models           TEXT NOT NULL DEFAULT '{}',
        allowed_tables   TEXT NOT NULL DEFAULT '[]',
        allowed_commands TEXT NOT NULL DEFAULT '[]',
        deny_rules       TEXT NOT NULL DEFAULT '[]',
        enabled          INTEGER NOT NULL DEFAULT 0,
        config           TEXT NOT NULL DEFAULT '{}',
        created_at       TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS secrets (
        name            TEXT PRIMARY KEY,
        encrypted_value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tasks_queue (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        from_agent          TEXT NOT NULL,
        to_agent            TEXT NOT NULL,
        payload             TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'taken', 'done', 'failed')),
        result              TEXT,
        chat_id             INTEGER,
        reply_to_message_id INTEGER,
        depth               INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_queue_to_status ON tasks_queue(to_agent, status, id);
    """,
    # 3: имена агентов, роли доступа, heartbeat, доставка результатов, аудит, подтверждения
    """
    ALTER TABLE agents ADD COLUMN display_name     TEXT;
    ALTER TABLE agents ADD COLUMN role_description TEXT;
    ALTER TABLE agents ADD COLUMN access_role      TEXT NOT NULL DEFAULT 'agent';
    ALTER TABLE agents ADD COLUMN created_by       TEXT;
    ALTER TABLE agents ADD COLUMN last_seen        TEXT;
    ALTER TABLE agents ADD COLUMN status           TEXT NOT NULL DEFAULT 'offline';
    ALTER TABLE agents ADD COLUMN status_detail    TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE agents ADD COLUMN last_error       TEXT;

    -- ничего не теряем: прежнее описание роли сохраняется в role_description
    UPDATE agents SET role_description = role WHERE role_description IS NULL;
    UPDATE agents SET display_name = 'Кимо', access_role = 'admin'   WHERE name = 'kimo';
    UPDATE agents SET display_name = 'Мимо', access_role = 'creator' WHERE name = 'agent_builder';
    UPDATE agents SET display_name = 'Мими'                          WHERE name = 'assistant';
    -- для остальных берём имя из config, только если оно похоже на имя (одно слово), а не на описание
    UPDATE agents SET display_name = json_extract(config, '$.display_name')
     WHERE display_name IS NULL AND json_valid(config)
       AND length(json_extract(config, '$.display_name')) BETWEEN 2 AND 20
       AND json_extract(config, '$.display_name') NOT LIKE '% %'
       AND json_extract(config, '$.display_name') NOT LIKE '%,%';
    -- агенты, созданные через Мимо (всё, кроме стартовых), записываются за ним
    UPDATE agents SET created_by = 'agent_builder'
     WHERE created_by IS NULL AND name NOT IN ('kimo', 'agent_builder', 'assistant');
    -- значения секретов не видит никто, в том числе Кимо
    UPDATE agents SET allowed_tables = (SELECT json_group_array(value) FROM json_each(allowed_tables) WHERE value <> 'secrets')
     WHERE name = 'kimo' AND json_valid(allowed_tables);

    -- таймаут задания для ассистента (веб-поиск дольше обычного), остальные — 60 с по умолчанию
    UPDATE agents SET config = json_set(config, '$.task_timeout_s', 180)
     WHERE name = 'assistant' AND json_valid(config) AND json_extract(config, '$.task_timeout_s') IS NULL;

    -- собственная таблица ассистента
    CREATE TABLE IF NOT EXISTS agent_assistant_notes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'note',
        text       TEXT NOT NULL
    );
    UPDATE agents SET allowed_tables = json_insert(allowed_tables, '$[#]', 'agent_assistant_notes')
     WHERE name = 'assistant' AND json_valid(allowed_tables)
       AND NOT EXISTS (SELECT 1 FROM json_each(allowed_tables) WHERE value = 'agent_assistant_notes');

    ALTER TABLE tasks_queue ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks_queue ADD COLUMN reply     INTEGER NOT NULL DEFAULT 0;
    UPDATE tasks_queue SET delivered = 1;  -- старые результаты повторно не доставляем
    CREATE INDEX IF NOT EXISTS idx_queue_undelivered ON tasks_queue(from_agent, status, delivered);

    CREATE TABLE IF NOT EXISTS audit_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        actor        TEXT NOT NULL,
        action       TEXT NOT NULL,
        target       TEXT,
        details      TEXT NOT NULL DEFAULT '{}',
        result       TEXT NOT NULL DEFAULT 'ok',
        confirmed_by INTEGER,
        report_id    INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
    CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target, action, ts);

    CREATE TABLE IF NOT EXISTS pending_actions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at     TEXT NOT NULL,
        expires_at     TEXT NOT NULL,
        actor          TEXT NOT NULL,
        action         TEXT NOT NULL,
        target         TEXT,
        params         TEXT NOT NULL DEFAULT '{}',
        summary        TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'new',
        result         TEXT,
        chat_id        INTEGER,
        expands_rights INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pending_actor ON pending_actions(actor, status, id);
    """,
    # 4: доходы (kind), флаги прав пользователей, настройки пользователя
    """
    ALTER TABLE expenses ADD COLUMN kind TEXT NOT NULL DEFAULT 'expense' CHECK (kind IN ('income', 'expense'));
    CREATE INDEX IF NOT EXISTS idx_expenses_user_kind ON expenses(user_id, kind, created_at);

    CREATE TABLE IF NOT EXISTS user_perms (
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        perm       TEXT NOT NULL,
        granted    INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, perm)
    );

    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key     TEXT NOT NULL,
        value   TEXT NOT NULL,
        PRIMARY KEY (user_id, key)
    );

    UPDATE agents SET allowed_tables = (
        SELECT json_group_array(value) FROM (
            SELECT value FROM json_each(agents.allowed_tables)
            UNION SELECT 'user_perms' UNION SELECT 'user_settings')
    ) WHERE name = 'kimo' AND json_valid(allowed_tables);
    """,
    # 5: Telegram Business — подключения, правила, чаты клиентов, журнал диалогов
    """
    CREATE TABLE IF NOT EXISTS business_connections (
        connection_id TEXT PRIMARY KEY,
        owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        user_chat_id  INTEGER,
        can_reply     INTEGER NOT NULL DEFAULT 0,
        is_enabled    INTEGER NOT NULL DEFAULT 1,
        ai_enabled    INTEGER NOT NULL DEFAULT 1,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_bconn_owner ON business_connections(owner_id);

    CREATE TABLE IF NOT EXISTS business_rules (
        owner_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        system_prompt TEXT NOT NULL DEFAULT '',
        business_info TEXT NOT NULL DEFAULT '',
        prohibitions  TEXT NOT NULL DEFAULT '',
        tone          TEXT NOT NULL DEFAULT '',
        owner_name    TEXT NOT NULL DEFAULT '',
        pause_hours   INTEGER NOT NULL DEFAULT 6,
        flood_per_min INTEGER NOT NULL DEFAULT 6,
        updated_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS business_chats (
        owner_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        chat_id          INTEGER NOT NULL,
        client_name      TEXT,
        username         TEXT,
        excluded         INTEGER NOT NULL DEFAULT 0,
        paused_until     TEXT,
        intro_sent       INTEGER NOT NULL DEFAULT 0,
        last_incoming_at TEXT,
        last_owner_at    TEXT,
        created_at       TEXT NOT NULL,
        PRIMARY KEY (owner_id, chat_id)
    );

    CREATE TABLE IF NOT EXISTS business_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        chat_id    INTEGER NOT NULL,
        role       TEXT NOT NULL CHECK (role IN ('client', 'assistant', 'owner')),
        text       TEXT NOT NULL,
        tg_msg_id  INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_bmsg_chat ON business_messages(owner_id, chat_id, id);
    CREATE INDEX IF NOT EXISTS idx_bmsg_created ON business_messages(created_at);

    UPDATE agents SET allowed_tables = (
        SELECT json_group_array(value) FROM (
            SELECT value FROM json_each(agents.allowed_tables)
            UNION SELECT 'business_connections' UNION SELECT 'business_rules'
            UNION SELECT 'business_chats' UNION SELECT 'business_messages')
    ) WHERE name = 'kimo' AND json_valid(allowed_tables);
    """,
    # 6: доступы по /grant (роль user), с автоотзывом по сроку
    """
    CREATE TABLE IF NOT EXISTS access_grants (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        granted_by  INTEGER NOT NULL DEFAULT 0,
        expires_at  TEXT NOT NULL,
        active      INTEGER NOT NULL DEFAULT 1,
        notified_1d INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        ended_at    TEXT,
        end_reason  TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_grants_one_active ON access_grants(user_id) WHERE active = 1;
    CREATE INDEX IF NOT EXISTS idx_grants_expires ON access_grants(active, expires_at);

    -- доступы, выданные прежней командой /grant (через users.sub_until), переносятся как активные
    INSERT OR IGNORE INTO access_grants (user_id, granted_by, expires_at, active, created_at)
        SELECT id, 0, sub_until, 1, created_at FROM users WHERE sub_until IS NOT NULL AND sub_until > datetime('now');

    UPDATE agents SET allowed_tables = (
        SELECT json_group_array(value) FROM (
            SELECT value FROM json_each(agents.allowed_tables) UNION SELECT 'access_grants')
    ) WHERE name = 'kimo' AND json_valid(allowed_tables);
    """,
    # 7: рассылки по расписанию, журнал отправок, подписчики через бота, отказы («стоп»)
    """
    CREATE TABLE IF NOT EXISTS scheduled_messages (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        chat_id       INTEGER NOT NULL,
        send_time     TEXT NOT NULL,
        weekdays      TEXT NOT NULL DEFAULT '1234567',
        tz            TEXT NOT NULL DEFAULT 'Asia/Tashkent',
        mode          TEXT NOT NULL DEFAULT 'text' CHECK (mode IN ('text', 'ai')),
        text          TEXT NOT NULL DEFAULT '',
        topic         TEXT NOT NULL DEFAULT '',
        channel       TEXT NOT NULL DEFAULT 'business' CHECK (channel IN ('business', 'bot')),
        active        INTEGER NOT NULL DEFAULT 1,
        last_run      TEXT,
        last_run_date TEXT,
        last_status   TEXT,
        created_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sched_owner ON scheduled_messages(owner_id, active);

    CREATE TABLE IF NOT EXISTS broadcast_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER,
        owner_id    INTEGER NOT NULL,
        chat_id     INTEGER NOT NULL,
        ts          TEXT NOT NULL,
        status      TEXT NOT NULL,
        detail      TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_blog_chat ON broadcast_log(owner_id, chat_id, ts);

    CREATE TABLE IF NOT EXISTS broadcast_subs (
        owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        chat_id    INTEGER NOT NULL,
        name       TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (owner_id, chat_id)
    );

    CREATE TABLE IF NOT EXISTS broadcast_optouts (
        owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        chat_id    INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (owner_id, chat_id)
    );

    UPDATE agents SET allowed_tables = (
        SELECT json_group_array(value) FROM (
            SELECT value FROM json_each(agents.allowed_tables)
            UNION SELECT 'scheduled_messages' UNION SELECT 'broadcast_log'
            UNION SELECT 'broadcast_subs' UNION SELECT 'broadcast_optouts')
    ) WHERE name = 'kimo' AND json_valid(allowed_tables);
    """,
]

# Откат миграций: номер -> SQL, возвращающий схему к предыдущей версии. Данные расходов и задач не трогаются.
DOWN_MIGRATIONS: dict[int, str] = {
    2: "DROP TABLE IF EXISTS tasks_queue; DROP TABLE IF EXISTS agents;",
    3: """
        DROP TABLE IF EXISTS pending_actions; DROP TABLE IF EXISTS audit_log;
        DROP TABLE IF EXISTS agent_assistant_notes;
        DROP INDEX IF EXISTS idx_queue_undelivered;
        ALTER TABLE tasks_queue DROP COLUMN delivered; ALTER TABLE tasks_queue DROP COLUMN reply;
        ALTER TABLE agents DROP COLUMN display_name; ALTER TABLE agents DROP COLUMN role_description;
        ALTER TABLE agents DROP COLUMN access_role;  ALTER TABLE agents DROP COLUMN created_by;
        ALTER TABLE agents DROP COLUMN last_seen;    ALTER TABLE agents DROP COLUMN status;
        ALTER TABLE agents DROP COLUMN status_detail; ALTER TABLE agents DROP COLUMN last_error;
    """,
    4: """
        DROP TABLE IF EXISTS user_settings; DROP TABLE IF EXISTS user_perms;
        DROP INDEX IF EXISTS idx_expenses_user_kind;
        ALTER TABLE expenses DROP COLUMN kind;
    """,
    7: """
        DROP TABLE IF EXISTS broadcast_optouts; DROP TABLE IF EXISTS broadcast_subs;
        DROP TABLE IF EXISTS broadcast_log; DROP TABLE IF EXISTS scheduled_messages;
    """,
    6: "DROP TABLE IF EXISTS access_grants;",
    5: """
        DROP TABLE IF EXISTS business_messages; DROP TABLE IF EXISTS business_chats;
        DROP TABLE IF EXISTS business_rules; DROP TABLE IF EXISTS business_connections;
    """,
}

AGENT_JSON_FIELDS = ("models", "allowed_tables", "allowed_commands", "deny_rules", "config", "status_detail")
_JSON_DICT_FIELDS = ("models", "config", "status_detail")
AGENT_COLUMNS = (
    "role", "bot_username", "system_prompt", "enabled", "display_name", "role_description", "access_role",
    "created_by", "last_seen", "status", "last_error", *AGENT_JSON_FIELDS,
)
ACCESS_ROLES = ("admin", "creator", "agent")


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

    def backup_dir(self) -> Path:
        return self.path.parent / "backups"

    async def backup(self, label: str = "manual") -> Path:
        """Согласованная копия БД (VACUUM INTO), безопасно при работающих агентах."""
        target_dir = self.backup_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = target_dir / f"{self.path.stem}-{stamp}-{label}.db"
        n = 1
        while target.exists():  # VACUUM INTO не пишет в существующий файл
            n += 1
            target = target_dir / f"{self.path.stem}-{stamp}-{label}-{n}.db"
        await self.conn.commit()
        await self.conn.execute("VACUUM INTO ?", (str(target),))
        _restrict_permissions(target)
        log.info("Бэкап БД: %s", target.name)
        return target

    async def migrate(self) -> None:
        """Применяет новые миграции под файловой блокировкой (агенты стартуют параллельно).
        Перед миграцией существующей БД делается бэкап."""
        lock_fd = os.open(f"{self.path}.migrate.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
            version = await self.schema_version()
            if version >= len(MIGRATIONS):
                return
            if version > 0:
                await self.backup(f"before-v{version + 1}")
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                try:
                    await self.conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
                except Exception:
                    await self.conn.rollback()
                    log.error("Миграция %s не применилась", number)
                    raise
                log.info("Применена миграция БД №%s", number)
            _restrict_permissions(self.path)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    async def downgrade(self, target: int) -> None:
        """Откат схемы до версии target (с бэкапом перед откатом)."""
        version = await self.schema_version()
        if target >= version:
            return
        await self.backup(f"before-downgrade-v{target}")
        for number in range(version, target, -1):
            script = DOWN_MIGRATIONS.get(number)
            if script is None:
                raise RuntimeError(f"Для миграции {number} нет отката — восстановите БД из бэкапа")
            await self.conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number - 1};\nCOMMIT;")
            log.info("Откачена миграция БД №%s", number)

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

    async def ensure_user(self, tg_id: int, now: str) -> dict[str, Any]:
        """Создать пользователя по tg_id, не трогая существующего."""
        await self._write("INSERT OR IGNORE INTO users (tg_id, created_at) VALUES (?, ?)", (tg_id, now))
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

    # ---------- права и настройки пользователя ----------

    async def user_perm_rows(self, user_id: int) -> dict[str, bool]:
        rows = await self._all("SELECT perm, granted FROM user_perms WHERE user_id = ?", (user_id,))
        return {r["perm"]: bool(r["granted"]) for r in rows}

    async def set_user_perm(self, user_id: int, perm: str, granted: bool, now: str) -> None:
        await self._write(
            """INSERT INTO user_perms (user_id, perm, granted, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, perm) DO UPDATE SET granted = excluded.granted, updated_at = excluded.updated_at""",
            (user_id, perm, int(granted), now),
        )

    async def get_user_setting(self, user_id: int, key: str) -> str | None:
        row = await self._one("SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (user_id, key))
        return row["value"] if row else None

    async def user_settings(self, user_id: int) -> dict[str, str]:
        rows = await self._all("SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,))
        return {r["key"]: r["value"] for r in rows}

    async def set_user_setting(self, user_id: int, key: str, value: str) -> None:
        await self._write(
            """INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value""",
            (user_id, key, value),
        )

    # ---------- Telegram Business ----------

    async def bconn_upsert(self, connection_id: str, owner_id: int, user_chat_id: int | None, can_reply: bool,
                           is_enabled: bool, now: str) -> None:
        await self._write(
            """INSERT INTO business_connections (connection_id, owner_id, user_chat_id, can_reply, is_enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(connection_id) DO UPDATE SET owner_id = excluded.owner_id, user_chat_id = excluded.user_chat_id,
                 can_reply = excluded.can_reply, is_enabled = excluded.is_enabled, updated_at = excluded.updated_at""",
            (connection_id, owner_id, user_chat_id, int(can_reply), int(is_enabled), now),
        )

    async def bconn_get(self, connection_id: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM business_connections WHERE connection_id = ?", (connection_id,))

    async def bconn_list(self, owner_id: int) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM business_connections WHERE owner_id = ? ORDER BY updated_at DESC", (owner_id,))

    async def bconn_set_ai(self, owner_id: int, enabled: bool) -> int:
        cur = await self._write("UPDATE business_connections SET ai_enabled = ? WHERE owner_id = ?", (int(enabled), owner_id))
        return cur.rowcount

    async def brules_get(self, owner_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM business_rules WHERE owner_id = ?", (owner_id,))

    async def brules_set(self, owner_id: int, now: str, **fields: Any) -> None:
        allowed = {"system_prompt", "business_info", "prohibitions", "tone", "owner_name", "pause_hours", "flood_per_min"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Неизвестные поля правил: {sorted(unknown)}")
        await self._write("INSERT OR IGNORE INTO business_rules (owner_id, updated_at) VALUES (?, ?)", (owner_id, now))
        for name, value in fields.items():
            await self._write(f"UPDATE business_rules SET {name} = ?, updated_at = ? WHERE owner_id = ?", (value, now, owner_id))

    async def bchat_touch(self, owner_id: int, chat_id: int, name: str | None, username: str | None, now: str,
                          incoming: bool) -> dict[str, Any]:
        await self._write(
            """INSERT INTO business_chats (owner_id, chat_id, client_name, username, created_at, last_incoming_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                 client_name = COALESCE(excluded.client_name, client_name), username = COALESCE(excluded.username, username),
                 last_incoming_at = CASE WHEN ? THEN excluded.last_incoming_at ELSE last_incoming_at END""",
            (owner_id, chat_id, name, username, now, now if incoming else None, int(incoming)),
        )
        chat = await self.bchat_get(owner_id, chat_id)
        assert chat is not None
        return chat

    async def bchat_get(self, owner_id: int, chat_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM business_chats WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id))

    async def bchat_list(self, owner_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM business_chats WHERE owner_id = ? ORDER BY COALESCE(last_incoming_at, created_at) DESC LIMIT ?",
            (owner_id, limit),
        )

    async def bchat_set(self, owner_id: int, chat_id: int, **fields: Any) -> bool:
        allowed = {"excluded", "paused_until", "intro_sent", "last_owner_at"}
        unknown = set(fields) - allowed
        if unknown or not fields:
            raise ValueError(f"Неизвестные поля чата: {sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = await self._write(
            f"UPDATE business_chats SET {sets} WHERE owner_id = ? AND chat_id = ?", (*fields.values(), owner_id, chat_id)
        )
        return cur.rowcount > 0

    async def bmsg_add(self, owner_id: int, chat_id: int, role: str, text: str, tg_msg_id: int | None, now: str) -> int:
        cur = await self._write(
            "INSERT INTO business_messages (owner_id, chat_id, role, text, tg_msg_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, role, text, tg_msg_id, now),
        )
        return int(cur.lastrowid)

    async def bmsg_recent(self, owner_id: int, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM business_messages WHERE owner_id = ? AND chat_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, chat_id, limit),
        )
        return rows[::-1]

    async def bmsg_is_ours(self, owner_id: int, chat_id: int, tg_msg_id: int) -> bool:
        row = await self._one(
            "SELECT 1 AS x FROM business_messages WHERE owner_id = ? AND chat_id = ? AND tg_msg_id = ? AND role = 'assistant'",
            (owner_id, chat_id, tg_msg_id),
        )
        return row is not None

    async def bmsg_edit(self, owner_id: int, chat_id: int, tg_msg_id: int, text: str) -> bool:
        cur = await self._write(
            "UPDATE business_messages SET text = ? WHERE owner_id = ? AND chat_id = ? AND tg_msg_id = ? AND role != 'assistant'",
            (text, owner_id, chat_id, tg_msg_id),
        )
        return cur.rowcount > 0

    async def bmsg_delete(self, owner_id: int, chat_id: int, tg_msg_ids: list[int]) -> int:
        n = 0
        for mid in tg_msg_ids:
            cur = await self._write(
                "DELETE FROM business_messages WHERE owner_id = ? AND chat_id = ? AND tg_msg_id = ?", (owner_id, chat_id, mid)
            )
            n += cur.rowcount
        return n

    async def bmsg_cleanup(self, before: str) -> int:
        cur = await self._write("DELETE FROM business_messages WHERE created_at < ?", (before,))
        return cur.rowcount

    # ---------- доступы (/grant) ----------

    async def grant_active(self, user_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM access_grants WHERE user_id = ? AND active = 1", (user_id,))

    async def grant_upsert(self, user_id: int, granted_by: int, expires_at: str, now: str) -> int:
        existing = await self.grant_active(user_id)
        if existing:
            await self._write(
                "UPDATE access_grants SET expires_at = ?, granted_by = ?, notified_1d = 0 WHERE id = ?",
                (expires_at, granted_by, existing["id"]),
            )
            return int(existing["id"])
        cur = await self._write(
            "INSERT INTO access_grants (user_id, granted_by, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (user_id, granted_by, expires_at, now),
        )
        return int(cur.lastrowid)

    async def grant_close(self, grant_id: int, reason: str, now: str) -> bool:
        cur = await self._write(
            "UPDATE access_grants SET active = 0, ended_at = ?, end_reason = ? WHERE id = ? AND active = 1",
            (now, reason, grant_id),
        )
        return cur.rowcount > 0

    async def grants_list_active(self, now: str) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT g.*, u.tg_id, u.username, u.first_name FROM access_grants g JOIN users u ON u.id = g.user_id
               WHERE g.active = 1 AND g.expires_at > ? ORDER BY g.expires_at""",
            (now,),
        )

    async def grants_expired(self, now: str) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT g.*, u.tg_id FROM access_grants g JOIN users u ON u.id = g.user_id
               WHERE g.active = 1 AND g.expires_at <= ?""",
            (now,),
        )

    async def grants_to_remind(self, now: str, until: str) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT g.*, u.tg_id FROM access_grants g JOIN users u ON u.id = g.user_id
               WHERE g.active = 1 AND g.notified_1d = 0 AND g.expires_at > ? AND g.expires_at <= ?""",
            (now, until),
        )

    async def grant_mark_notified(self, grant_id: int) -> None:
        await self._write("UPDATE access_grants SET notified_1d = 1 WHERE id = ?", (grant_id,))

    # ---------- рассылки ----------

    async def sched_create(self, owner_id: int, chat_id: int, send_time: str, weekdays: str, tz: str, mode: str,
                           text: str, topic: str, channel: str, now: str) -> int:
        cur = await self._write(
            """INSERT INTO scheduled_messages (owner_id, chat_id, send_time, weekdays, tz, mode, text, topic, channel, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, chat_id, send_time, weekdays, tz, mode, text, topic, channel, now),
        )
        return int(cur.lastrowid)

    async def sched_get(self, owner_id: int, sched_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM scheduled_messages WHERE id = ? AND owner_id = ?", (sched_id, owner_id))

    async def sched_list(self, owner_id: int) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM scheduled_messages WHERE owner_id = ? ORDER BY id", (owner_id,))

    async def sched_all_active(self) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM scheduled_messages WHERE active = 1 ORDER BY id")

    async def sched_update(self, owner_id: int, sched_id: int, **fields: Any) -> bool:
        allowed = {"chat_id", "send_time", "weekdays", "tz", "mode", "text", "topic", "channel", "active",
                   "last_run", "last_run_date", "last_status"}
        unknown = set(fields) - allowed
        if unknown or not fields:
            raise ValueError(f"Неизвестные поля рассылки: {sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = await self._write(
            f"UPDATE scheduled_messages SET {sets} WHERE id = ? AND owner_id = ?", (*fields.values(), sched_id, owner_id)
        )
        return cur.rowcount > 0

    async def sched_delete(self, owner_id: int, sched_id: int) -> bool:
        cur = await self._write("DELETE FROM scheduled_messages WHERE id = ? AND owner_id = ?", (sched_id, owner_id))
        return cur.rowcount > 0

    async def sched_disable_chat(self, owner_id: int, chat_id: int) -> int:
        cur = await self._write(
            "UPDATE scheduled_messages SET active = 0, last_status = 'stopped: клиент написал «стоп»' "
            "WHERE owner_id = ? AND chat_id = ? AND active = 1",
            (owner_id, chat_id),
        )
        return cur.rowcount

    async def blog_add(self, schedule_id: int | None, owner_id: int, chat_id: int, status: str, detail: str, now: str) -> int:
        cur = await self._write(
            "INSERT INTO broadcast_log (schedule_id, owner_id, chat_id, ts, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (schedule_id, owner_id, chat_id, now, status, detail[:300]),
        )
        return int(cur.lastrowid)

    async def blog_list(self, owner_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM broadcast_log WHERE owner_id = ? ORDER BY id DESC LIMIT ?", (owner_id, limit))

    async def blog_sent_since(self, owner_id: int, chat_id: int, since: str) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM broadcast_log WHERE owner_id = ? AND chat_id = ? AND status = 'sent' AND ts >= ?",
            (owner_id, chat_id, since),
        )
        return int(row["n"]) if row else 0

    async def bsub_add(self, owner_id: int, chat_id: int, name: str | None, now: str) -> None:
        await self._write("INSERT OR IGNORE INTO broadcast_subs (owner_id, chat_id, name, created_at) VALUES (?, ?, ?, ?)",
                          (owner_id, chat_id, name, now))
        await self._write("DELETE FROM broadcast_optouts WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id))

    async def bsub_get(self, owner_id: int, chat_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM broadcast_subs WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id))

    async def bsub_list(self, owner_id: int) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM broadcast_subs WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,))

    async def bsub_owners_of(self, chat_id: int) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM broadcast_subs WHERE chat_id = ?", (chat_id,))

    async def bsub_remove(self, owner_id: int, chat_id: int) -> bool:
        cur = await self._write("DELETE FROM broadcast_subs WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id))
        return cur.rowcount > 0

    async def optout_add(self, owner_id: int, chat_id: int, now: str) -> None:
        await self._write("INSERT OR IGNORE INTO broadcast_optouts (owner_id, chat_id, created_at) VALUES (?, ?, ?)",
                          (owner_id, chat_id, now))

    async def optout_has(self, owner_id: int, chat_id: int) -> bool:
        return await self._one("SELECT 1 AS x FROM broadcast_optouts WHERE owner_id = ? AND chat_id = ?",
                               (owner_id, chat_id)) is not None

    async def optout_remove(self, owner_id: int, chat_id: int) -> bool:
        cur = await self._write("DELETE FROM broadcast_optouts WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id))
        return cur.rowcount > 0

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

    async def upcoming_reminders(self, user_id: int, start: str, end: str) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT * FROM tasks WHERE user_id = ? AND has_time = 1 AND done_at IS NULL
               AND due_at >= ? AND due_at < ? ORDER BY due_at LIMIT 30""",
            (user_id, start, end),
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
        kind: str = "expense",
    ) -> int:
        if kind not in ("income", "expense"):
            raise ValueError("kind должен быть income или expense")
        cur = await self._write(
            """INSERT INTO expenses (user_id, amount, currency, category, note, rate_used, created_at, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, amount, currency, category, note, rate_used, now, kind),
        )
        return int(cur.lastrowid)

    async def expenses_between(self, user_id: int, start: str, end: str, kind: str | None = "expense") -> list[dict[str, Any]]:
        """kind='expense' (по умолчанию), 'income' или None — все операции."""
        if kind is None:
            return await self._all(
                "SELECT * FROM expenses WHERE user_id = ? AND created_at >= ? AND created_at < ? ORDER BY created_at",
                (user_id, start, end),
            )
        return await self._all(
            "SELECT * FROM expenses WHERE user_id = ? AND kind = ? AND created_at >= ? AND created_at < ? ORDER BY created_at",
            (user_id, kind, start, end),
        )

    async def last_expense(self, user_id: int, kind: str | None = None) -> dict[str, Any] | None:
        if kind is None:
            return await self._one("SELECT * FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        return await self._one(
            "SELECT * FROM expenses WHERE user_id = ? AND kind = ? ORDER BY id DESC LIMIT 1", (user_id, kind)
        )

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

    # ---------- агенты (реестр) ----------

    @staticmethod
    def _agent_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        for field in AGENT_JSON_FIELDS:
            try:
                row[field] = json.loads(row.get(field) or ("{}" if field in _JSON_DICT_FIELDS else "[]"))
            except (TypeError, ValueError):
                row[field] = {} if field in _JSON_DICT_FIELDS else []
        row["enabled"] = bool(row["enabled"])
        return row

    async def insert_agent(self, agent: dict[str, Any], now: str, replace: bool = False) -> bool:
        """Добавить агента. replace=False — не трогать существующего (seed). True, если запись изменилась."""
        access_role = agent.get("access_role", "agent")
        if access_role not in ACCESS_ROLES:
            raise ValueError(f"access_role: {', '.join(ACCESS_ROLES)}")
        values = [agent["name"], agent["role"], agent.get("bot_username"), agent.get("system_prompt", ""),
                  json.dumps(agent.get("models", {}), ensure_ascii=False),
                  json.dumps(agent.get("allowed_tables", []), ensure_ascii=False),
                  json.dumps(agent.get("allowed_commands", []), ensure_ascii=False),
                  json.dumps(agent.get("deny_rules", []), ensure_ascii=False),
                  int(bool(agent.get("enabled", False))),
                  json.dumps(agent.get("config", {}), ensure_ascii=False), now,
                  agent.get("display_name"), agent.get("role_description") or agent["role"], access_role,
                  agent.get("created_by"), agent.get("last_seen"), agent.get("status", "offline"),
                  json.dumps(agent.get("status_detail", {}), ensure_ascii=False), agent.get("last_error")]
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        cur = await self._write(
            f"""{verb} INTO agents (name, role, bot_username, system_prompt, models, allowed_tables,
                allowed_commands, deny_rules, enabled, config, created_at, display_name, role_description,
                access_role, created_by, last_seen, status, status_detail, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(values),
        )
        return cur.rowcount > 0

    async def get_agent(self, name: str) -> dict[str, Any] | None:
        return self._agent_row(await self._one("SELECT * FROM agents WHERE name = ?", (name,)))

    async def list_agents(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agents" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY created_at, name"
        return [self._agent_row(r) for r in await self._all(sql)]  # type: ignore[misc]

    async def update_agent(self, name: str, **fields: Any) -> bool:
        unknown = set(fields) - set(AGENT_COLUMNS)
        if unknown:
            raise ValueError(f"Неизвестные поля агента: {sorted(unknown)}")
        if not fields:
            return False
        sets, params = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")  # имена колонок только из белого списка AGENT_COLUMNS
            if key in AGENT_JSON_FIELDS:
                value = json.dumps(value, ensure_ascii=False)
            elif key == "enabled":
                value = int(bool(value))
            elif key == "access_role" and value not in ACCESS_ROLES:
                raise ValueError(f"access_role: {', '.join(ACCESS_ROLES)}")
            params.append(value)
        cur = await self._write(f"UPDATE agents SET {', '.join(sets)} WHERE name = ?", (*params, name))
        return cur.rowcount > 0

    async def delete_agent(self, name: str) -> bool:
        cur = await self._write("DELETE FROM agents WHERE name = ?", (name,))
        return cur.rowcount > 0

    # ---------- шина заданий ----------

    async def queue_insert(
        self,
        from_agent: str,
        to_agent: str,
        payload: dict[str, Any],
        now: str,
        *,
        chat_id: int | None = None,
        reply_to_message_id: int | None = None,
        depth: int = 0,
        status: str = "new",
        result: str | None = None,
        reply: bool = False,
        delivered: bool = False,
    ) -> int:
        cur = await self._write(
            """INSERT INTO tasks_queue (from_agent, to_agent, payload, status, result, chat_id,
               reply_to_message_id, depth, created_at, updated_at, reply, delivered)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (from_agent, to_agent, json.dumps(payload, ensure_ascii=False), status, result, chat_id,
             reply_to_message_id, depth, now, now, int(reply), int(delivered)),
        )
        return int(cur.lastrowid)

    async def queue_claim(self, to_agent: str, now: str) -> dict[str, Any] | None:
        """Атомарно забрать самое старое новое задание агента. Два процесса не получат одно задание."""
        for _ in range(5):
            row = await self._one(
                "SELECT id FROM tasks_queue WHERE to_agent = ? AND status = 'new' ORDER BY id LIMIT 1", (to_agent,)
            )
            if row is None:
                return None
            cur = await self._write(
                "UPDATE tasks_queue SET status = 'taken', updated_at = ? WHERE id = ? AND status = 'new'",
                (now, row["id"]),
            )
            if cur.rowcount == 1:
                return await self.queue_get(row["id"])
        return None

    async def queue_get(self, task_id: int) -> dict[str, Any] | None:
        row = await self._one("SELECT * FROM tasks_queue WHERE id = ?", (task_id,))
        if row:
            try:
                row["payload"] = json.loads(row["payload"])
            except ValueError:
                row["payload"] = {}
        return row

    async def queue_finish(self, task_id: int, status: str, result: str | None, now: str) -> bool:
        """Завершить взятое задание. False — задание уже закрыто (например, watchdog пометил failed по таймауту)."""
        if status not in ("done", "failed"):
            raise ValueError("status: done | failed")
        cur = await self._write(
            "UPDATE tasks_queue SET status = ?, result = ?, updated_at = ? WHERE id = ? AND status = 'taken'",
            (status, result, now, task_id),
        )
        return cur.rowcount > 0

    async def queue_force_fail(self, task_id: int, result: str, now: str) -> bool:
        """Принудительно закрыть новое/взятое задание (watchdog). False — уже закрыто."""
        cur = await self._write(
            "UPDATE tasks_queue SET status = 'failed', result = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('new', 'taken')",
            (result, now, task_id),
        )
        return cur.rowcount > 0

    async def queue_mark_delivered(self, task_id: int) -> bool:
        """Атомарно: True получает только тот, кто первым отметил результат доставленным."""
        cur = await self._write("UPDATE tasks_queue SET delivered = 1 WHERE id = ? AND delivered = 0", (task_id,))
        return cur.rowcount > 0

    async def queue_unmark_delivered(self, task_id: int) -> None:
        await self._write("UPDATE tasks_queue SET delivered = 0 WHERE id = ?", (task_id,))

    async def queue_undelivered(self, from_agent: str, limit: int = 20) -> list[dict[str, Any]]:
        """Завершённые задания агента, результат которых ещё не отправлен в чат."""
        rows = await self._all(
            """SELECT * FROM tasks_queue WHERE from_agent = ? AND status IN ('done', 'failed')
               AND delivered = 0 AND reply = 1 ORDER BY id LIMIT ?""",
            (from_agent, limit),
        )
        for row in rows:
            try:
                row["payload"] = json.loads(row["payload"])
            except ValueError:
                row["payload"] = {}
        return rows

    async def queue_open(self, statuses: tuple[str, ...] = ("new", "taken"), limit: int = 200) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        rows = await self._all(
            f"SELECT * FROM tasks_queue WHERE status IN ({marks}) ORDER BY id LIMIT ?", (*statuses, limit)
        )
        for row in rows:
            try:
                row["payload"] = json.loads(row["payload"])
            except ValueError:
                row["payload"] = {}
        return rows

    async def queue_counts(self) -> list[dict[str, Any]]:
        return await self._all("SELECT to_agent, status, COUNT(*) AS n FROM tasks_queue GROUP BY to_agent, status")

    async def queue_recent_failed(self, limit: int = 5, to_agent: str | None = None) -> list[dict[str, Any]]:
        if to_agent:
            return await self._all(
                "SELECT * FROM tasks_queue WHERE status = 'failed' AND to_agent = ? ORDER BY id DESC LIMIT ?",
                (to_agent, limit),
            )
        return await self._all("SELECT * FROM tasks_queue WHERE status = 'failed' ORDER BY id DESC LIMIT ?", (limit,))

    async def queue_fail_interrupted(self, to_agent: str, now: str) -> int:
        """Задания, взятые агентом до перезапуска, помечаются failed (не повторяем: возможны дубли действий)."""
        cur = await self._write(
            "UPDATE tasks_queue SET status = 'failed', result = 'Прервано перезапуском агента', updated_at = ? "
            "WHERE to_agent = ? AND status = 'taken'",
            (now, to_agent),
        )
        return cur.rowcount

    async def queue_cleanup(self, before: str) -> int:
        cur = await self._write(
            "DELETE FROM tasks_queue WHERE status IN ('done', 'failed') AND updated_at < ?", (before,)
        )
        return cur.rowcount

    # ---------- heartbeat ----------

    async def agent_heartbeat(
        self, name: str, status: str, now: str, last_error: str | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        """Агент сообщает о себе. last_error=None не стирает прошлую ошибку, пустая строка стирает."""
        if status not in ("online", "busy", "error", "offline"):
            raise ValueError("status: online | busy | error | offline")
        await self._write(
            """UPDATE agents SET last_seen = ?, status = ?, status_detail = ?,
               last_error = CASE WHEN ? IS NULL THEN last_error WHEN ? = '' THEN NULL ELSE ? END WHERE name = ?""",
            (now, status, json.dumps(detail or {}, ensure_ascii=False), last_error, last_error, last_error, name),
        )

    # ---------- аудит и подтверждения ----------

    async def audit(
        self,
        ts: str,
        actor: str,
        action: str,
        target: str | None = None,
        details: dict[str, Any] | None = None,
        result: str = "ok",
        confirmed_by: int | None = None,
        report_id: int | None = None,
    ) -> int:
        cur = await self._write(
            """INSERT INTO audit_log (ts, actor, action, target, details, result, confirmed_by, report_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, actor, action, target, json.dumps(details or {}, ensure_ascii=False), result, confirmed_by, report_id),
        )
        return int(cur.lastrowid)

    async def audit_list(
        self, limit: int = 20, target: str | None = None, action: str | None = None, since: str | None = None
    ) -> list[dict[str, Any]]:
        where, params = ["1 = 1"], []
        for col, val in (("target", target), ("action", action)):
            if val is not None:
                where.append(f"{col} = ?")  # col — из фиксированного набора выше
                params.append(val)
        if since:
            where.append("ts >= ?")
            params.append(since)
        return await self._all(
            f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", (*params, limit)
        )

    async def pending_create(
        self,
        actor: str,
        action: str,
        target: str | None,
        params: dict[str, Any],
        summary: str,
        now: str,
        expires_at: str,
        chat_id: int | None = None,
        expands_rights: bool = False,
    ) -> int:
        cur = await self._write(
            """INSERT INTO pending_actions (created_at, expires_at, actor, action, target, params, summary,
               chat_id, expands_rights) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, expires_at, actor, action, target, json.dumps(params, ensure_ascii=False), summary, chat_id,
             int(expands_rights)),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _pending_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row:
            try:
                row["params"] = json.loads(row["params"])
            except ValueError:
                row["params"] = {}
        return row

    async def pending_get(self, pending_id: int) -> dict[str, Any] | None:
        return self._pending_row(await self._one("SELECT * FROM pending_actions WHERE id = ?", (pending_id,)))

    async def pending_take(self, pending_id: int, new_status: str, now: str) -> bool:
        """Атомарно перевести new -> confirmed/rejected. Повторное нажатие кнопки ничего не делает."""
        cur = await self._write(
            "UPDATE pending_actions SET status = ? WHERE id = ? AND status = 'new' AND expires_at > ?",
            (new_status, pending_id, now),
        )
        return cur.rowcount > 0

    async def pending_finish(self, pending_id: int, status: str, result: str | None) -> None:
        await self._write("UPDATE pending_actions SET status = ?, result = ? WHERE id = ?", (status, result, pending_id))

    async def pending_open(self, actor: str, now: str) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM pending_actions WHERE actor = ? AND status = 'new' AND expires_at > ? ORDER BY id",
            (actor, now),
        )
        return [self._pending_row(r) for r in rows]  # type: ignore[misc]

    async def pending_expire(self, now: str) -> int:
        cur = await self._write(
            "UPDATE pending_actions SET status = 'expired' WHERE status = 'new' AND expires_at <= ?", (now,)
        )
        return cur.rowcount

    # ---------- структура БД (только для слоя admin) ----------

    async def table_names(self) -> list[str]:
        rows = await self._all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        return [r["name"] for r in rows]

    async def table_columns(self, table: str) -> list[dict[str, Any]]:
        if table not in await self.table_names():  # имя таблицы только из sqlite_master
            raise ValueError("нет такой таблицы")
        return await self._all(f'PRAGMA table_info("{table}")')

    async def table_rows(self, table: str, columns: list[str], limit: int, offset: int = 0) -> list[dict[str, Any]]:
        if table not in await self.table_names():
            raise ValueError("нет такой таблицы")
        real = {c["name"] for c in await self.table_columns(table)}
        cols = [c for c in columns if c in real]
        if not cols:
            return []
        select = ", ".join(f'"{c}"' for c in cols)  # имена колонок только из PRAGMA
        return await self._all(f'SELECT {select} FROM "{table}" ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset))

    async def table_update_row(self, table: str, row_id: int, column: str, value: Any) -> bool:
        if table not in await self.table_names() or column not in {c["name"] for c in await self.table_columns(table)}:
            raise ValueError("нет такой таблицы или колонки")
        cur = await self._write(f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?', (value, row_id))
        return cur.rowcount > 0

    # ---------- собственная таблица ассистента ----------

    async def add_note(self, kind: str, text: str, now: str) -> int:
        cur = await self._write(
            "INSERT INTO agent_assistant_notes (created_at, kind, text) VALUES (?, ?, ?)", (now, kind, text)
        )
        return int(cur.lastrowid)

    async def list_notes(self, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        if kind:
            return await self._all(
                "SELECT * FROM agent_assistant_notes WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)
            )
        return await self._all("SELECT * FROM agent_assistant_notes ORDER BY id DESC LIMIT ?", (limit,))

    async def delete_note(self, note_id: int) -> bool:
        cur = await self._write("DELETE FROM agent_assistant_notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0
