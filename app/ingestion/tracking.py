from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingested_documents (
    tenant_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    last_ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, file_path)
);
"""


async def ensure_tracking_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def compute_file_hash(path: Path) -> str:
    """算文件内容的 sha256，用来判断一个文件相对上次摄取有没有变化——
    不看修改时间（mtime 在文件被复制/迁移时会变，即使内容完全没变），
    只看内容本身。
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def get_tracked_hash(
    conn: aiosqlite.Connection, *, tenant_id: str, file_path: str
) -> str | None:
    cursor = await conn.execute(
        "SELECT content_hash FROM ingested_documents WHERE tenant_id = ? AND file_path = ?",
        (tenant_id, file_path),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def record_ingested(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    file_path: str,
    content_hash: str,
    chunk_count: int,
) -> None:
    await conn.execute(
        "INSERT INTO ingested_documents "
        "(tenant_id, file_path, content_hash, chunk_count, last_ingested_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(tenant_id, file_path) DO UPDATE SET "
        "content_hash=excluded.content_hash, chunk_count=excluded.chunk_count, "
        "last_ingested_at=datetime('now')",
        (tenant_id, file_path, content_hash, chunk_count),
    )
    await conn.commit()


async def list_tracked_files(
    conn: aiosqlite.Connection, *, tenant_id: str, limit: int | None = None, offset: int = 0
) -> list[dict[str, Any]]:
    """limit=None（默认）返回该租户全部追踪记录，保持既有调用方（摄取
    管线、scan_changes、eval runner 等）不传这两个参数时的行为不变；管理
    后台分页时显式传入具体的 limit/offset。哨兵模式与
    app/graphrag/review_queue.py::list_pending_reviews 一致：SQLite 的
    LIMIT 取负数即表示不限制行数，用 -1 承载 limit=None 这个语义。

    原查询没有 ORDER BY，这里补上 ORDER BY last_ingested_at DESC 让分页
    结果顺序稳定可预期——没有确定排序的分页会在翻页之间出现同一条记录
    在两页都出现或者漏掉的问题。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT file_path, content_hash, chunk_count, last_ingested_at "
        "FROM ingested_documents WHERE tenant_id = ? ORDER BY last_ingested_at DESC "
        "LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_tracked_files(conn: aiosqlite.Connection, *, tenant_id: str) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM ingested_documents WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    return row[0]


async def remove_tracked_file(
    conn: aiosqlite.Connection, *, tenant_id: str, file_path: str
) -> None:
    await conn.execute(
        "DELETE FROM ingested_documents WHERE tenant_id = ? AND file_path = ?",
        (tenant_id, file_path),
    )
    await conn.commit()
