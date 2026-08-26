from __future__ import annotations

from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS duplicate_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    candidate_a_node_key TEXT NOT NULL,
    candidate_b_node_key TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_duplicate_review_queue_status
    ON duplicate_review_queue (tenant_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_duplicate_review_queue_pair
    ON duplicate_review_queue (tenant_id, candidate_a_node_key, candidate_b_node_key)
    WHERE status = 'pending';
"""


class DuplicateReviewNotFoundError(Exception):
    """review_id 不存在（或不属于这个 tenant_id）。"""


class DuplicateReviewAlreadyResolvedError(Exception):
    """这条记录已经被批准/驳回过，不能重复处理。"""


async def ensure_duplicate_review_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def enqueue_duplicate_suggestion(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    candidate_a_node_key: str,
    candidate_b_node_key: str,
    similarity_score: float,
    reason: str,
) -> None:
    """INSERT OR IGNORE——撞到 idx_duplicate_review_queue_pair 唯一索引
    （这一对已经有一条 pending 记录）时静默跳过，不报错，批跑重复调用不产生
    重复建议。"""
    await conn.execute(
        "INSERT OR IGNORE INTO duplicate_review_queue "
        "(tenant_id, candidate_a_node_key, candidate_b_node_key, similarity_score, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (tenant_id, candidate_a_node_key, candidate_b_node_key, similarity_score, reason),
    )
    await conn.commit()


async def list_pending_duplicate_suggestions(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM duplicate_review_queue WHERE tenant_id = ? AND status = 'pending' "
        "ORDER BY similarity_score DESC, review_id LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_pending_duplicate_suggestions(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM duplicate_review_queue WHERE tenant_id = ? AND status = 'pending'",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return row[0]


async def has_any_duplicate_record(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    candidate_a_node_key: str,
    candidate_b_node_key: str,
) -> bool:
    """顺序不敏感——批跑 worker 每次两两比对时，候选对的先后顺序不保证
    跟上次入队时一致，两个方向都要查。"""
    cursor = await conn.execute(
        "SELECT 1 FROM duplicate_review_queue WHERE tenant_id = ? AND "
        "((candidate_a_node_key = ? AND candidate_b_node_key = ?) OR "
        " (candidate_a_node_key = ? AND candidate_b_node_key = ?)) LIMIT 1",
        (tenant_id, candidate_a_node_key, candidate_b_node_key,
         candidate_b_node_key, candidate_a_node_key),
    )
    row = await cursor.fetchone()
    return row is not None


async def _fetch_pending_row(
    conn: aiosqlite.Connection, review_id: int, *, tenant_id: str
) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM duplicate_review_queue WHERE review_id = ? AND tenant_id = ?",
        (review_id, tenant_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise DuplicateReviewNotFoundError(f"待审核记录不存在: {review_id}")
    row_dict = dict(row)
    if row_dict["status"] != "pending":
        raise DuplicateReviewAlreadyResolvedError(
            f"待审核记录已处理过 (status={row_dict['status']}): {review_id}"
        )
    return row_dict


async def approve_duplicate_suggestion(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    tenant_id: str,
    keep_node_key: str,
) -> None:
    """keep_node_key 是候选对里保留哪一条的 node_key（必须是这条记录的
    candidate_a_node_key/candidate_b_node_key 之一）。实际的合并写入（墓碑化
    被合并那条、把它原本的 standard_name+aliases 追加进保留那条、失败时
    补偿恢复）委托给 terms_store.merge_terms()——那是 terms 表自己的操作，
    这一层只负责把"候选对里的哪个是 merged"这个审核队列特有的判断做完，
    再把 terms_store.TermNotFoundError 翻译成这条队列自己的
    DuplicateReviewNotFoundError（其它异常，比如 TermNameConflictError，
    原样透传给调用方——那不是"审核记录本身有问题"，是"合并写入本身失败
    了"，调用方按自己的语义处理）。"""
    from app.graphrag import terms_store

    row = await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    candidates = {row["candidate_a_node_key"], row["candidate_b_node_key"]}
    if keep_node_key not in candidates:
        raise ValueError(
            f"keep_node_key {keep_node_key!r} 不是这条记录的候选之一: {candidates}"
        )
    merged_node_key = next(nk for nk in candidates if nk != keep_node_key)

    try:
        await terms_store.merge_terms(
            conn,
            tenant_id=tenant_id,
            keep_node_key=keep_node_key,
            merged_node_key=merged_node_key,
        )
    except terms_store.TermNotFoundError as exc:
        raise DuplicateReviewNotFoundError(
            f"候选术语不存在（可能已被删除）: keep={keep_node_key!r}, merged={merged_node_key!r}"
        ) from exc

    await conn.execute(
        "UPDATE duplicate_review_queue SET status='approved', "
        "resolved_at=datetime('now'), resolved_note=? "
        "WHERE review_id=? AND tenant_id=?",
        (f"merged {merged_node_key} into {keep_node_key}", review_id, tenant_id),
    )
    await conn.commit()


async def reject_duplicate_suggestion(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    tenant_id: str,
    note: str | None = None,
) -> None:
    await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    await conn.execute(
        "UPDATE duplicate_review_queue SET status='rejected', "
        "resolved_at=datetime('now'), resolved_note=? "
        "WHERE review_id=? AND tenant_id=?",
        (note, review_id, tenant_id),
    )
    await conn.commit()
