from __future__ import annotations

from typing import Any

import aiosqlite


async def append_turn(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    user_id: str,
    role: str,
    content: str,
) -> None:
    await conn.execute(
        "INSERT INTO conversation_turns (session_id, user_id, role, content) "
        "VALUES (?, ?, ?, ?)",
        (session_id, user_id, role, content),
    )
    await conn.commit()


async def get_recent_turns(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """返回该会话最近 limit 条消息，按时间正序排列。"""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT role, content, created_at FROM conversation_turns "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in reversed(rows)]
