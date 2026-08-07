from __future__ import annotations

from typing import Any, Protocol

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_candidate TEXT NOT NULL,
    object_candidate TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_review_queue_status
    ON graph_review_queue (status);
"""


class ReviewGraphClientProtocol(Protocol):
    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
    ) -> None: ...


class ReviewNotFoundError(Exception):
    """指定的 review_id 在队列里不存在。"""


class ReviewAlreadyResolvedError(Exception):
    """指定的 review_id 已经被批准或驳回过，不能重复处理。"""


async def ensure_review_schema(conn: aiosqlite.Connection) -> None:
    """幂等建表，可重复调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def enqueue_for_review(
    conn: aiosqlite.Connection,
    *,
    subject_candidate: str,
    object_candidate: str,
    relation_type: str,
    reason: str,
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    这里存的是 LLM 抽取出的原始候选名（subject_candidate/object_candidate），
    不是标准名——正是因为它们对不上术语表才会进队列，所以此时还没有标准名可存。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason) "
        "VALUES (?, ?, ?, ?)",
        (subject_candidate, object_candidate, relation_type, reason),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_pending_reviews(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, created_at FROM graph_review_queue "
        "WHERE status = 'pending' ORDER BY review_id"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _fetch_pending_row(
    conn: aiosqlite.Connection, review_id: int
) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM graph_review_queue WHERE review_id = ?", (review_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        raise ReviewNotFoundError(f"待审核记录不存在: {review_id}")
    row_dict = dict(row)
    if row_dict["status"] != "pending":
        raise ReviewAlreadyResolvedError(
            f"待审核记录已处理过 (status={row_dict['status']}): {review_id}"
        )
    return row_dict


async def approve_review(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    subject_standard_name: str,
    object_standard_name: str,
    graph_client: ReviewGraphClientProtocol,
) -> None:
    """人工确认候选关系对应的标准名称后，写入图谱并把队列状态标记为已批准。

    subject_standard_name/object_standard_name 必须由人工审核时指定——
    正是因为自动归一化时这两个候选名没能命中术语表才会进队列，这里不能
    再退回自动解析，必须由人明确给出（通常意味着术语表也要同步补充别名）。
    """
    row = await _fetch_pending_row(conn, review_id)
    await graph_client.merge_relation(
        subject_standard_name=subject_standard_name,
        object_standard_name=object_standard_name,
        relation_type=row["relation_type"],
    )
    await conn.execute(
        "UPDATE graph_review_queue SET status='approved', "
        "resolved_at=datetime('now'), resolved_note=? WHERE review_id=?",
        (f"{subject_standard_name} -> {object_standard_name}", review_id),
    )
    await conn.commit()


async def reject_review(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    note: str | None = None,
) -> None:
    """人工判定该候选是噪声/误抽取，标记驳回，不写入图谱。"""
    await _fetch_pending_row(conn, review_id)
    await conn.execute(
        "UPDATE graph_review_queue SET status='rejected', "
        "resolved_at=datetime('now'), resolved_note=? WHERE review_id=?",
        (note, review_id),
    )
    await conn.commit()
