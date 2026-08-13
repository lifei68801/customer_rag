from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.terms_store import ensure_terms_schema


async def build_review_conn_from_settings(settings: Settings) -> aiosqlite.Connection:
    db_path = Path(settings.graph_review_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    try:
        await ensure_review_schema(conn)
        await ensure_terms_schema(conn, seed_yaml_path=Path(settings.terminology_path))
    except Exception:
        await conn.close()
        raise
    return conn
