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
    terms_module: Any = None,
) -> None:
    """keep_node_key 是候选对里保留哪一条的 node_key（必须是这条记录的
    candidate_a_node_key/candidate_b_node_key 之一）。被合并那条 Term 的
    standard_name，连同它自己已有的全部 aliases，一起追加进保留那条的
    aliases（去重）——不是只追加 standard_name，否则被合并那条自己的别名
    会变成孤儿，resolve_term() 再也找不回它们。被合并那条 Term 本身不删除
    （node_key 可能已经被 Neo4j 图数据引用，删除会破坏引用完整性），只是
    它的 standard_name 变成了保留那条的一个 alias。

    terms_module 默认使用 app.graphrag.terms_store（真实的 list_terms/
    update_term）；测试可以传一个提供同名异步方法的 fake 对象，不需要真的
    建一张 terms 表。
    """
    if terms_module is None:
        from app.graphrag import terms_store as terms_module
    row = await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    candidates = {row["candidate_a_node_key"], row["candidate_b_node_key"]}
    if keep_node_key not in candidates:
        raise ValueError(
            f"keep_node_key {keep_node_key!r} 不是这条记录的候选之一: {candidates}"
        )
    merged_node_key = next(nk for nk in candidates if nk != keep_node_key)

    terms = await terms_module.list_terms(conn, tenant_id)
    terms_by_node_key = {t.node_key: t for t in terms}
    keep_term = terms_by_node_key.get(keep_node_key)
    merged_term = terms_by_node_key.get(merged_node_key)
    if keep_term is None or merged_term is None:
        raise DuplicateReviewNotFoundError(
            f"候选术语不存在（可能已被删除）: keep={keep_node_key!r}, merged={merged_node_key!r}"
        )

    merged_aliases = list(dict.fromkeys(
        [*keep_term.aliases, merged_term.standard_name, *merged_term.aliases]
    ))
    await terms_module.update_term(
        conn,
        tenant_id=tenant_id,
        standard_name=keep_term.standard_name,
        new_standard_name=keep_term.standard_name,
        aliases=merged_aliases,
        term_type=keep_term.term_type,
        extra_properties=keep_term.extra_properties,
        current_term_type=keep_term.term_type,
    )
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
