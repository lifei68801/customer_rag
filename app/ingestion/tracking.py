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
    conn: aiosqlite.Connection, *, tenant_id: str
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT file_path, content_hash, chunk_count, last_ingested_at "
        "FROM ingested_documents WHERE tenant_id = ?",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def remove_tracked_file(
    conn: aiosqlite.Connection, *, tenant_id: str, file_path: str
) -> None:
    await conn.execute(
        "DELETE FROM ingested_documents WHERE tenant_id = ? AND file_path = ?",
        (tenant_id, file_path),
    )
    await conn.commit()
