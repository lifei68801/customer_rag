from __future__ import annotations

import json
from typing import Any

import aiosqlite


async def upsert_memory_item(
    conn: aiosqlite.Connection,
    *,
    memory_id: str,
    tenant_id: str,
    user_id: str,
    text: str,
    confidence: float = 0.8,
    embedding: list[float] | None = None,
) -> None:
    embedding_json = json.dumps(embedding) if embedding is not None else None
    await conn.execute(
        "INSERT INTO memory_items "
        "(memory_id, tenant_id, user_id, text, confidence, embedding_json, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', datetime('now')) "
        "ON CONFLICT(memory_id) DO UPDATE SET "
        "text=excluded.text, confidence=excluded.confidence, "
        "embedding_json=excluded.embedding_json, "
        "status='active', updated_at=datetime('now')",
        (memory_id, tenant_id, user_id, text, confidence, embedding_json),
    )
    await conn.commit()


async def list_active_memory_items(
    conn: aiosqlite.Connection, *, tenant_id: str, user_id: str
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT memory_id, text, confidence, embedding_json, updated_at FROM memory_items "
        "WHERE tenant_id = ? AND user_id = ? AND status = 'active' ORDER BY updated_at",
        (tenant_id, user_id),
    )
    rows = await cursor.fetchall()
    items = []
    for row in rows:
        item = dict(row)
        embedding_json = item.pop("embedding_json")
        item["embedding"] = json.loads(embedding_json) if embedding_json else None
        items.append(item)
    return items


async def mark_deleted(
    conn: aiosqlite.Connection, *, memory_id: str, tenant_id: str, user_id: str
) -> None:
    await conn.execute(
        "UPDATE memory_items SET status='deleted', updated_at=datetime('now') "
        "WHERE memory_id = ? AND tenant_id = ? AND user_id = ?",
        (memory_id, tenant_id, user_id),
    )
    await conn.commit()


async def append_history(
    conn: aiosqlite.Connection,
    *,
    memory_id: str,
    tenant_id: str,
    user_id: str,
    event: str,
    old_text: str | None,
    new_text: str | None,
    reason: str | None,
    conflict_type: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO memory_history "
        "(memory_id, tenant_id, user_id, event, old_text, new_text, reason, conflict_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, tenant_id, user_id, event, old_text, new_text, reason, conflict_type),
    )
    await conn.commit()
