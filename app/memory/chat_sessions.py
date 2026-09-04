from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite

_TITLE_MAX_LENGTH = 30


def _derive_title(first_message: str) -> str:
    text = first_message.strip()
    if len(text) <= _TITLE_MAX_LENGTH:
        return text
    return text[:_TITLE_MAX_LENGTH] + "…"


async def touch_session(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    first_message: str,
    now: datetime,
) -> None:
    """记录/刷新左边栏会话列表要用的元信息：首次出现的 session_id 用
    first_message（当轮用户问题）截断出标题并插入一行；已存在则只刷新
    updated_at，不覆盖标题——标题永远来自这个会话的第一条用户消息，不随
    后续消息变化。

    调用方（app/agent/graph.py::memory_save_node）每轮对话只调一次，不是
    每条 turn（一问一答）都调——updated_at 只需要反映"这轮对话发生的时间"，
    调两次没有额外信息量，还多一次写。
    """
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    await conn.execute(
        "INSERT INTO chat_sessions (tenant_id, session_id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, session_id) DO UPDATE SET updated_at = excluded.updated_at",
        (tenant_id, session_id, user_id, _derive_title(first_message), now_str, now_str),
    )
    await conn.commit()


async def list_sessions(
    conn: aiosqlite.Connection, *, tenant_id: str, user_id: str
) -> list[dict[str, Any]]:
    """按最近活跃时间倒序返回该租户该用户名下的全部会话。"""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT session_id, title, created_at, updated_at FROM chat_sessions "
        "WHERE tenant_id = ? AND user_id = ? ORDER BY updated_at DESC",
        (tenant_id, user_id),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def session_belongs_to_user(
    conn: aiosqlite.Connection, *, tenant_id: str, user_id: str, session_id: str
) -> bool:
    """这个会话是不是这个租户下这个用户的。

    归属信息本来就在 chat_sessions 里（touch_session 每轮都写），
    delete_session 早就把 user_id 当成删除条件了；读消息那一侧原先只按
    tenant_id+session_id 查，于是同租户的另一个坐席猜中/拿到 session_id
    就能读到整段对话。这个函数是那条读路径缺的归属判据。
    """
    cursor = await conn.execute(
        "SELECT 1 FROM chat_sessions WHERE tenant_id = ? AND user_id = ? AND session_id = ?",
        (tenant_id, user_id, session_id),
    )
    return await cursor.fetchone() is not None


async def delete_session(
    conn: aiosqlite.Connection, *, tenant_id: str, user_id: str, session_id: str
) -> bool:
    """真删除：会话元信息 + 该会话下全部对话轮次一起删，返回是否真的删到了
    一行（用于路由层区分 404 和 200）。user_id 是删除条件的一部分——只能
    删自己名下的会话，猜中别人的 session_id 也删不掉。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "DELETE FROM chat_sessions WHERE tenant_id = ? AND user_id = ? AND session_id = ?",
        (tenant_id, user_id, session_id),
    )
    deleted = cursor.rowcount > 0
    await conn.execute(
        "DELETE FROM conversation_turns WHERE tenant_id = ? AND user_id = ? AND session_id = ?",
        (tenant_id, user_id, session_id),
    )
    await conn.commit()
    return deleted
