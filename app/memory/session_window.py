from __future__ import annotations

from typing import Any

import aiosqlite


async def append_turn(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    role: str,
    content: str,
) -> None:
    await conn.execute(
        "INSERT INTO conversation_turns (tenant_id, session_id, user_id, role, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (tenant_id, session_id, user_id, role, content),
    )
    await conn.commit()


async def get_recent_turns(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """返回该租户该会话最近 limit 条消息，按时间正序排列。"""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT role, content, created_at FROM conversation_turns "
        "WHERE tenant_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?",
        (tenant_id, session_id, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in reversed(rows)]


async def get_turns_with_ids(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """带上 conversation_turns.id 的轮次，按时间正序。

    跟 get_recent_turns 分开而不是给它加一列：那个函数的返回值会直接
    `SessionMessage(**turn)` 进 API 响应（app/api/session_routes.py），多一个
    内部自增 id 会顺着响应体流出去。这里的 id 是会话摘要用来标记"摘到哪儿
    为止"的内部游标，只在服务端流转。

    limit=None 表示取全部——增量摘要要按全部轮次算切点，只看最近 N 条的话，
    比 N 更早的那些永远不会进摘要。
    """
    conn.row_factory = aiosqlite.Row
    if limit is None:
        cursor = await conn.execute(
            "SELECT id, role, content, created_at FROM conversation_turns "
            "WHERE tenant_id = ? AND session_id = ? ORDER BY id",
            (tenant_id, session_id),
        )
        return [dict(row) for row in await cursor.fetchall()]
    cursor = await conn.execute(
        "SELECT id, role, content, created_at FROM conversation_turns "
        "WHERE tenant_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?",
        (tenant_id, session_id, limit),
    )
    return [dict(row) for row in reversed(await cursor.fetchall())]
