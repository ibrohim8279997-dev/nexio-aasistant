"""Служебные команды.

    python manage.py db version              — версия схемы БД
    python manage.py db backup               — бэкап БД в data/backups/
    python manage.py db backups              — список бэкапов
    python manage.py db downgrade <версия>   — откат миграций (с бэкапом перед откатом)
    python manage.py db restore <файл> --yes — восстановить БД из бэкапа (сначала остановите агентов)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

from bot.config import BASE_DIR, ConfigError
from bot.db import MIGRATIONS, Database


def db_path() -> Path:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
    path = Path(os.getenv("DB_PATH", "").strip() or "data/bot.db")
    return path if path.is_absolute() else BASE_DIR / path


async def _with_db(func):
    db = await Database.connect(db_path())
    try:
        return await func(db)
    finally:
        await db.close()


def cmd_db(args: argparse.Namespace) -> int:
    path = db_path()
    if args.action == "version":
        async def show(db: Database) -> None:
            print(f"Схема: v{await db.schema_version()} (последняя в коде: v{len(MIGRATIONS)})")
        asyncio.run(_with_db(show))
    elif args.action == "backup":
        target = asyncio.run(_with_db(lambda db: db.backup("manual")))
        print(f"Бэкап: {target}")
    elif args.action == "backups":
        backups = sorted((path.parent / "backups").glob("*.db"))
        print("\n".join(str(b) for b in backups) or "Бэкапов нет")
    elif args.action == "downgrade":
        if args.arg is None or not args.arg.isdigit():
            print("Укажите версию: python manage.py db downgrade 1")
            return 1
        asyncio.run(_with_db(lambda db: db.downgrade(int(args.arg))))
        print(f"Схема откачена до v{args.arg}")
    elif args.action == "restore":
        if not args.arg or not Path(args.arg).is_file():
            print("Укажите файл бэкапа: python manage.py db restore data/backups/....db --yes")
            return 1
        if not args.yes:
            print("Остановите агентов (./run_all.sh stop) и повторите с --yes")
            return 1
        asyncio.run(_with_db(lambda db: db.backup("before-restore")))
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        shutil.copy2(args.arg, path)
        os.chmod(path, 0o600)
        print(f"БД восстановлена из {args.arg}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Служебные команды бота")
    sub = parser.add_subparsers(dest="group", required=True)
    p_db = sub.add_parser("db")
    p_db.add_argument("action", choices=["version", "backup", "backups", "downgrade", "restore"])
    p_db.add_argument("arg", nargs="?")
    p_db.add_argument("--yes", action="store_true")
    p_db.set_defaults(func=cmd_db)
    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
