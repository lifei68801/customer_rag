from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pending_clarifications (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    original_question TEXT NOT NULL,
    clarification_prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, session_id)
);
"""

# 只覆盖客服场景里最常见的"这就是在回答时间追问"的短语模式：常见时间词
# （日期数字、周几、今天/昨天/上周等）+ 整体很短（不像是在问一个新问题）。
# 不追求覆盖所有可能的时间表达，只要比"完全不判断"更有用。
_TIME_LIKE_PATTERN = re.compile(
    r"(今天|昨天|前天|明天|上午|下午|晚上|早上|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"上周|上个月|这周|本周|\d+月\d*号?|\d+月\d*日|\d+号|\d+日)"
)
_MAX_TIME_REPLY_LENGTH = 10


async def ensure_clarification_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def set_pending_clarification(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    original_question: str,
    clarification_prompt: str,
    now: datetime,
    ttl_seconds: int = 300,
) -> None:
    """记录"待澄清"状态，带 TTL。同一个会话再次触发澄清时直接覆盖旧状态——
    只保留最近一次待澄清的问题，不维护多个并存的待澄清队列。
    """
    expires_at = now.timestamp() + ttl_seconds
    await conn.execute(
        "INSERT INTO pending_clarifications "
        "(tenant_id, session_id, original_question, clarification_prompt, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, session_id) DO UPDATE SET "
        "original_question=excluded.original_question, "
        "clarification_prompt=excluded.clarification_prompt, "
        "created_at=excluded.created_at, expires_at=excluded.expires_at",
        (
            tenant_id,
            session_id,
            original_question,
            clarification_prompt,
            now.isoformat(),
            expires_at,
        ),
    )
    await conn.commit()


async def get_pending_clarification(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    session_id: str,
    now: datetime,
) -> dict[str, Any] | None:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM pending_clarifications WHERE tenant_id = ? AND session_id = ?",
        (tenant_id, session_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    item = dict(row)
    if now.timestamp() > float(item["expires_at"]):
        return None
    return item


async def clear_pending_clarification(
    conn: aiosqlite.Connection, *, tenant_id: str, session_id: str
) -> None:
    await conn.execute(
        "DELETE FROM pending_clarifications WHERE tenant_id = ? AND session_id = ?",
        (tenant_id, session_id),
    )
    await conn.commit()


def looks_like_a_time_reply(text: str) -> bool:
    """粗略判断这轮用户输入是不是"只回答了一个时间"，而不是提了个新问题。

    判定依据：命中常见时间词模式，且整体长度很短——单纯一个"上周五"
    符合，一整句"我想问网络连不上怎么办"即使碰巧包含数字也不该被误判。
    """
    stripped = text.strip()
    if len(stripped) > _MAX_TIME_REPLY_LENGTH:
        return False
    return bool(_TIME_LIKE_PATTERN.search(stripped))


def merge_clarification_reply(*, original_question: str, reply_text: str) -> str:
    """把用户对澄清追问的回复拼接回原始问题，重新组成一个完整问题去检索，
    不需要用户重复描述一遍完整问题。"""
    return f"{original_question}（{reply_text}）"
