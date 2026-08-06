from __future__ import annotations

from datetime import datetime

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticket_fix_notifications (
    ticket_id TEXT NOT NULL,
    fix_id TEXT NOT NULL,
    notified_at REAL NOT NULL,
    PRIMARY KEY (ticket_id, fix_id)
);
"""


async def ensure_ticket_fix_notifications_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def is_already_notified(conn: aiosqlite.Connection, *, ticket_id: str, fix_id: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM ticket_fix_notifications WHERE ticket_id = ? AND fix_id = ?",
        (ticket_id, fix_id),
    )
    row = await cursor.fetchone()
    return row is not None


async def mark_notified(
    conn: aiosqlite.Connection, *, ticket_id: str, fix_id: str, now: datetime
) -> None:
    await conn.execute(
        "INSERT INTO ticket_fix_notifications (ticket_id, fix_id, notified_at) "
        "VALUES (?, ?, ?) ON CONFLICT(ticket_id, fix_id) DO NOTHING",
        (ticket_id, fix_id, now.timestamp()),
    )
    await conn.commit()
