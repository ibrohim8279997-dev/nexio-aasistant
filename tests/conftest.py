from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import Database  # noqa: E402


@pytest.fixture
async def db(tmp_path: Path):
    database = await Database.connect(tmp_path / "test.db")
    await database.migrate()
    yield database
    await database.close()
