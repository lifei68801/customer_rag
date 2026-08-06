from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite

from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorRecord

_SQLITE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


async def query_turns_in_window(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """按客户ID+时间窗口查询原始对话轮次，跨 session_id（"上周的会话"
    大概率不是当前 session）。architecture doc §6.3 P1 结构化历史检索。

    created_at 比较用字符串格式（和 conversation_turns 表 created_at
    列的 SQLite `datetime('now')` 默认值格式一致：'YYYY-MM-DD HH:MM:SS'），
    不解析成 datetime 对象再比较——避免引入时区/精度不一致的转换风险。
    """
    conn.row_factory = aiosqlite.Row
    start_str = start.strftime(_SQLITE_DATETIME_FORMAT)
    end_str = end.strftime(_SQLITE_DATETIME_FORMAT)
    cursor = await conn.execute(
        "SELECT session_id, role, content, created_at FROM conversation_turns "
        "WHERE tenant_id = ? AND user_id = ? AND created_at BETWEEN ? AND ? "
        "ORDER BY created_at",
        (tenant_id, user_id, start_str, end_str),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def search_turns_by_keyword_and_window(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    start: datetime,
    end: datetime,
    question: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """在时间窗口内的轮次基础上，再按当前问题做一次关键词过滤——窗口
    可能跨多天多个会话，不加这层过滤会把大量不相关对话也拼进上下文。
    """
    turns = await query_turns_in_window(
        conn, tenant_id=tenant_id, user_id=user_id, start=start, end=end
    )
    if not turns:
        return []

    records = [
        VectorRecord(
            id=str(index), vector=[], text=turn["content"], tenant_id=tenant_id, metadata={}
        )
        for index, turn in enumerate(turns)
    ]
    bm25_index = BM25Index()
    bm25_index.index(records)
    hits = bm25_index.search(question, top_k=top_k, tenant_id=tenant_id)
    hit_indices = [int(hit.id) for hit in hits]
    return [turns[index] for index in hit_indices]
