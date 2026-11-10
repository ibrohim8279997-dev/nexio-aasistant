"""Управление процессами агентов. Только заранее заданные действия: старт и остановка окна tmux одного агента.

Произвольные shell-команды из текста модели не выполняются никогда. Остальных агентов, run_all.sh и базу
эти функции не затрагивают. Реализацию можно подменить (set_control), например в тестах.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from bot.agents.health import effective_status
from bot.agents.registry import NAME_RE
from bot.config import BASE_DIR
from bot.db import Database
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

ONLINE_TIMEOUT = 60.0


class ProcessControl(Protocol):
    async def start(self, name: str) -> tuple[bool, str]: ...

    async def stop(self, name: str) -> None: ...


class TmuxControl:
    """./run.sh start|stop <агент>: отдельное окно tmux, остальные агенты не затрагиваются."""

    async def _run(self, action: str, name: str, timeout: float) -> tuple[bool, str]:
        if not NAME_RE.match(name):
            return False, "некорректное имя агента"
        try:
            proc = await asyncio.create_subprocess_exec(
                str(BASE_DIR / "run.sh"), action, name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(BASE_DIR),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            return False, type(exc).__name__
        return proc.returncode == 0, out.decode(errors="replace").strip()[-300:]

    async def start(self, name: str) -> tuple[bool, str]:
        return await self._run("start", name, 30)

    async def stop(self, name: str) -> None:
        ok, message = await self._run("stop", name, 15)
        if not ok:
            log.warning("Не удалось остановить агента %s: %s", name, message)


_control: ProcessControl = TmuxControl()


def get_control() -> ProcessControl:
    return _control


def set_control(control: ProcessControl) -> None:
    global _control
    _control = control


async def wait_online(db: Database, name: str, since: str, timeout: float = ONLINE_TIMEOUT, interval: float = 1.0) -> bool:
    """Ждать heartbeat нового процесса: last_seen не раньше since и статус не offline."""
    deadline = time.monotonic() + timeout
    while True:
        row = await db.get_agent(name)
        if row and row["last_seen"] and row["last_seen"] >= since and effective_status(row["last_seen"], row["status"]) != "offline":
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


def stamp() -> str:
    return to_db(now_utc())
