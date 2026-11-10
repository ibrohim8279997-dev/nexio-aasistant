"""Имитация процессов агентов: «tmux» запускает настоящие Heartbeat и BusWorker в asyncio-задачах."""
from __future__ import annotations

import asyncio
from pathlib import Path

from bot.agents.bus import Bus, BusWorker
from bot.agents.health import AgentState, Heartbeat
from bot.timeutil import now_utc, to_db
from tests.fakebot import FakeBot


class SimControl:
    def __init__(self, db, logs: Path, owner_id: int) -> None:
        self.db, self.logs, self.owner_id = db, logs, owner_id
        self.calls: list[tuple[str, str]] = []
        self.always_fail: set[str] = set()  # агент не поднимается, что бы ни меняли в конфиге
        self._tasks: dict[str, list[asyncio.Task]] = {}
        self._parts: dict[str, tuple[Heartbeat, BusWorker]] = {}

    async def start(self, name: str):
        self.calls.append(("start", name))
        row = await self.db.get_agent(name)
        broken = name in self.always_fail or not (row["models"] or {}).get("text")
        if broken:  # окно tmux открылось, но процесс сразу упал: в логе ошибка конфигурации
            self.logs.mkdir(exist_ok=True)
            with open(self.logs / f"{name}.log", "a", encoding="utf-8") as fh:
                fh.write("2026-11-10 09:00:00,000 ERROR bot: Ошибка конфигурации: нет текстовой модели\n")
            return True, "window opened"
        state = AgentState()
        heartbeat = Heartbeat(self.db, name, state, interval=0.2)
        async def handler(task):
            return "pong" if (task["payload"] or {}).get("type") == "ping" else "ok"
        worker = BusWorker(Bus(self.db, name, owner_id=self.owner_id), handler, FakeBot(), self.owner_id, interval=0.05, state=state)
        self._parts[name] = (heartbeat, worker)
        self._tasks[name] = [asyncio.create_task(heartbeat.run()), asyncio.create_task(worker.run())]
        return True, "started"

    async def stop(self, name: str):
        self.calls.append(("stop", name))
        parts = self._parts.pop(name, None)
        if parts:
            heartbeat, worker = parts
            heartbeat.stop()
            worker.stop()
            await asyncio.gather(*self._tasks.pop(name), return_exceptions=True)
        else:
            await self.db.agent_heartbeat(name, "offline", to_db(now_utc()))

    async def shutdown(self):
        for name in list(self._parts):
            await self.stop(name)
