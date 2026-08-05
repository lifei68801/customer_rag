from __future__ import annotations

from typing import Any

import aiosqlite


async def upsert_memory_item(
    conn: aiosqlite.Connection,
    *,
    memory_id: str,
    user_id: str,
    text: str,
    confidence: float = 0.8,
) -> None:
    await conn.execute(
        "INSERT INTO memory_items (memory_id, user_id, text, confidence, status, updated_at) "
        "VALUES (?, ?, ?, ?, 'active', datetime('now')) "
        "ON CONFLICT(memory_id) DO UPDATE SET "
        "text=excluded.text, confidence=excluded.confidence, "
        "status='active', updated_at=datetime('now')",
        (memory_id, user_id, text, confidence),
    )
    await conn.commit()


async def list_active_memory_items(
    conn: aiosqlite.Connection, *, user_id: str
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT memory_id, text, confidence, updated_at FROM memory_items "
        "WHERE user_id = ? AND status = 'active' ORDER BY updated_at",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_deleted(
    conn: aiosqlite.Connection, *, memory_id: str, user_id: str
) -> None:
    await conn.execute(
        "UPDATE memory_items SET status='deleted', updated_at=datetime('now') "
        "WHERE memory_id = ? AND user_id = ?",
        (memory_id, user_id),
    )
    await conn.commit()


async def append_history(
    conn: aiosqlite.Connection,
    *,
    memory_id: str,
    user_id: str,
    event: str,
    old_text: str | None,
    new_text: str | None,
    reason: str | None,
) -> None:
    await conn.execute(
        "INSERT INTO memory_history (memory_id, user_id, event, old_text, new_text, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (memory_id, user_id, event, old_text, new_text, reason),
    )
    await conn.commit()
