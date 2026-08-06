from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import aiosqlite

from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS known_fixes (
    fix_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    description TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    fixed_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_known_fixes_tenant ON known_fixes (tenant_id);
"""


async def ensure_known_fixes_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def register_known_fix(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    description: str,
    fixed_at: datetime,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
) -> str:
    """登记一条已知故障修复，供 scan_and_send_known_fix_followups() 匹配
    历史工单。这是一次性的管理操作（人工/管理员触发），embedding 调用
    失败时让异常直接上抛——操作者需要知道登记失败了，不能静默丢弃。
    """
    embed_result = await embedding_registry.run(
        EmbeddingRequest(texts=[description]), provider_name=embedding_provider_name
    )
    fix_id = str(uuid.uuid4())
    await ensure_known_fixes_schema(conn)
    await conn.execute(
        "INSERT INTO known_fixes (fix_id, tenant_id, description, embedding_json, fixed_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            fix_id,
            tenant_id,
            description,
            json.dumps(embed_result.vectors[0]),
            fixed_at.timestamp(),
            datetime.now().timestamp(),
        ),
    )
    await conn.commit()
    return fix_id


async def list_known_fixes(conn: aiosqlite.Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM known_fixes WHERE tenant_id = ?", (tenant_id,)
    )
    rows = await cursor.fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["embedding"] = json.loads(item.pop("embedding_json"))
        results.append(item)
    return results
