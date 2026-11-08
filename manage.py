"""Служебные команды.

    python manage.py db version              — версия схемы БД
    python manage.py db backup               — бэкап БД в data/backups/
    python manage.py db backups              — список бэкапов
    python manage.py db downgrade <версия>   — откат миграций (с бэкапом перед откатом)
    python manage.py db restore <файл> --yes — восстановить БД из бэкапа (сначала остановите агентов)

    python manage.py agents list             — все агенты
    python manage.py agents enabled          — имена включённых (для run_all.sh)
    python manage.py agents show <имя>       — карточка агента (JSON)
    python manage.py agents enable|disable <имя>
    python manage.py agents sync [имя]       — перечитать agents/*.json в БД (enabled и bot_username сохраняются)
    python manage.py agents add <файл.json>  — добавить агента из файла (выключенным)
    python manage.py agents set-token <имя>  — сохранить токен бота зашифрованным в secrets (ввод скрыт)
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

TOKEN_RE = r"^\d{6,12}:[A-Za-z0-9_-]{30,}$"


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


def cmd_agents(args: argparse.Namespace) -> int:
    import getpass
    import json
    import re

    from bot.agents.registry import NAME_RE, Registry, load_default_configs
    from bot.timeutil import now_utc, to_db

    async def run(db: Database) -> int:
        await db.migrate()
        reg = Registry(db)
        await reg.seed_defaults()
        name = args.arg
        if args.action in ("show", "enable", "disable", "set-token") and (not name or not NAME_RE.match(name)):
            print("Укажите имя агента (латиница, цифры, _)")
            return 1
        if args.action == "list":
            for a in await reg.all():
                mark = "✅" if a.enabled else "⏸"
                print(f"{mark} {a.name:<16} @{a.bot_username or '-':<20} {a.display_name}: {a.role[:70]}")
        elif args.action == "enabled":
            for a in await reg.all(enabled_only=True):
                print(a.name)
        elif args.action == "show":
            row = await db.get_agent(name)
            print(json.dumps(row, ensure_ascii=False, indent=2) if row else "Нет такого агента")
        elif args.action in ("enable", "disable"):
            ok = await db.update_agent(name, enabled=args.action == "enable")
            print("Готово" if ok else "Нет такого агента")
        elif args.action == "sync":
            print("Обновлены:", ", ".join(await reg.sync_from_files([name] if name else None)) or "—")
        elif args.action == "add":
            if not name or not Path(name).is_file():
                print("Укажите JSON-файл карточки")
                return 1
            cards = [c for c in load_default_configs(Path(name).parent) if Path(name).stem == c["name"]]
            card = cards[0] if cards else json.loads(Path(name).read_text(encoding="utf-8"))
            if not NAME_RE.match(str(card.get("name", ""))) or not card.get("role"):
                print("В карточке нужны name (латиница) и role")
                return 1
            card["enabled"] = False
            added = await db.insert_agent(card, to_db(now_utc()))
            print("Добавлен (выключен). Включите: python manage.py agents enable " + card["name"] if added else "Уже есть")
        elif args.action == "set-token":
            from bot.config import load_settings
            from bot.crypto import Crypto, SecretStore

            token = getpass.getpass(f"Токен бота {name} (ввод скрыт): ").strip()
            if not re.match(TOKEN_RE, token):
                print("Это не похоже на токен BotFather")
                return 1
            store = SecretStore(db, Crypto(load_settings().encryption_key))
            await store.set(f"bot_token:{name}", token)
            print(f"Токен {name} сохранён в secrets (зашифрован)")
        return 0

    return asyncio.run(_with_db(run))


def main() -> int:
    parser = argparse.ArgumentParser(description="Служебные команды бота")
    sub = parser.add_subparsers(dest="group", required=True)
    p_db = sub.add_parser("db")
    p_db.add_argument("action", choices=["version", "backup", "backups", "downgrade", "restore"])
    p_db.add_argument("arg", nargs="?")
    p_db.add_argument("--yes", action="store_true")
    p_db.set_defaults(func=cmd_db)
    p_ag = sub.add_parser("agents")
    p_ag.add_argument("action", choices=["list", "enabled", "show", "enable", "disable", "sync", "add", "set-token"])
    p_ag.add_argument("arg", nargs="?")
    p_ag.set_defaults(func=cmd_agents)
    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
