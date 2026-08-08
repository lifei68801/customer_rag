from __future__ import annotations

from typing import Any, Protocol

import aiosqlite

from app.db_migrations import add_column_if_missing

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_candidate TEXT NOT NULL,
    object_candidate TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    suggested_subject_standard_name TEXT,
    suggested_object_standard_name TEXT,
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
    """指定的 review_id 在该租户下不存在（包括存在于别的租户名下的情况——
    不做区分，统一按"不存在"处理，避免向调用方泄露"这个 id 属于别的租户"
    这个信息）。"""


class ReviewAlreadyResolvedError(Exception):
    """指定的 review_id 已经被批准或驳回过，不能重复处理。"""


async def ensure_review_schema(conn: aiosqlite.Connection) -> None:
    """幂等建表+迁移，可重复调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    # tenant_id 迁移历史数据默认回填 'demo'——项目里目前唯一真实产生过
    # 数据的租户就是 demo，见 docs/superpowers/specs/2026-08-08-admin-backend-design.md 第2节。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="tenant_id",
        ddl="TEXT NOT NULL DEFAULT 'demo'",
    )
    # source 记录候选关系抽取自哪个文档，approve_review 批准时要把它传给
    # graph_client.merge_relation()（写入图谱边的 source 属性，用于文档
    # 重新摄取时按 source 清理旧边）。历史数据没有这个信息，回填空字符串
    # （不是 NULL，避免下游拼接/比较时到处判空）。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="source",
        ddl="TEXT NOT NULL DEFAULT ''",
    )


async def enqueue_for_review(
    conn: aiosqlite.Connection,
    *,
    subject_candidate: str,
    object_candidate: str,
    relation_type: str,
    reason: str,
    source: str,
    tenant_id: str,
    suggested_subject_standard_name: str | None = None,
    suggested_object_standard_name: str | None = None,
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    source/tenant_id 是批准时写入图谱边所必需的信息，来自调用方
    normalize_and_write_relations() 本身已有的同名参数，这里改为必填，
    不给默认值——遗漏它们会让批准动作在写图谱这一步直接失败。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason, "
        "suggested_subject_standard_name, suggested_object_standard_name, "
        "source, tenant_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject_candidate,
            object_candidate,
            relation_type,
            reason,
            suggested_subject_standard_name,
            suggested_object_standard_name,
            source,
            tenant_id,
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_pending_reviews(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "source, created_at FROM graph_review_queue "
        "WHERE status = 'pending' AND tenant_id = ? ORDER BY review_id",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_resolved_reviews(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查已批准/已驳回的历史记录，按 resolved_at 倒序（最近处理的排前面）。

    status 为 None 时返回 approved+rejected 两种都算"已处理"的记录；
    传 'approved'/'rejected' 时只看其中一种。
    """
    conn.row_factory = aiosqlite.Row
    if status is None:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, created_at "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status IN ('approved', 'rejected') "
            "ORDER BY resolved_at DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, created_at "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status = ? "
            "ORDER BY resolved_at DESC LIMIT ? OFFSET ?",
            (tenant_id, status, limit, offset),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _fetch_pending_row(
    conn: aiosqlite.Connection, review_id: int, *, tenant_id: str
) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM graph_review_queue WHERE review_id = ? AND tenant_id = ?",
        (review_id, tenant_id),
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
    tenant_id: str,
    graph_client: ReviewGraphClientProtocol,
) -> None:
    """人工确认候选关系对应的标准名称后，写入图谱并把队列状态标记为已批准。"""
    row = await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    await graph_client.merge_relation(
        subject_standard_name=subject_standard_name,
        object_standard_name=object_standard_name,
        relation_type=row["relation_type"],
        source=row["source"],
        tenant_id=tenant_id,
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
    tenant_id: str,
    note: str | None = None,
) -> None:
    """人工判定该候选是噪声/误抽取，标记驳回，不写入图谱。"""
    await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    await conn.execute(
        "UPDATE graph_review_queue SET status='rejected', "
        "resolved_at=datetime('now'), resolved_note=? WHERE review_id=?",
        (note, review_id),
    )
    await conn.commit()
