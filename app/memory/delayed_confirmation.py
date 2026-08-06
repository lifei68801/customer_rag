from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS delayed_confirmations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    context TEXT NOT NULL,
    confirm_after REAL NOT NULL,
    confirmed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_delayed_confirmations_due
    ON delayed_confirmations (tenant_id, confirmed_at, confirm_after);
"""


async def ensure_delayed_confirmation_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def schedule_delayed_confirmation(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    context: str,
    confirm_after: datetime,
) -> str:
    confirmation_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO delayed_confirmations (id, tenant_id, user_id, context, confirm_after) "
        "VALUES (?, ?, ?, ?, ?)",
        (confirmation_id, tenant_id, user_id, context, confirm_after.timestamp()),
    )
    await conn.commit()
    return confirmation_id


async def list_due_confirmations(
    conn: aiosqlite.Connection, *, tenant_id: str, now: datetime
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM delayed_confirmations WHERE tenant_id = ? "
        "AND confirmed_at IS NULL AND confirm_after <= ? ORDER BY confirm_after",
        (tenant_id, now.timestamp()),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_confirmed(conn: aiosqlite.Connection, *, confirmation_id: str, now: datetime) -> None:
    await conn.execute(
        "UPDATE delayed_confirmations SET confirmed_at = ? WHERE id = ?",
        (now.timestamp(), confirmation_id),
    )
    await conn.commit()
