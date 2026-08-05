from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.memory.schema import ensure_schema


async def build_memory_conn_from_settings(settings: Settings) -> aiosqlite.Connection:
    db_path = Path(settings.memory_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    await ensure_schema(conn)
    return conn
