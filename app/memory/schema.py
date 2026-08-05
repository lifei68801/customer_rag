from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_session
    ON conversation_turns (tenant_id, session_id, id);

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL NOT NULL DEFAULT 0.8,
    embedding_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memory_items_user
    ON memory_items (tenant_id, user_id, status);

CREATE TABLE IF NOT EXISTS memory_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    event TEXT NOT NULL,
    old_text TEXT,
    new_text TEXT,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS consolidation_jobs (
    job_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_input TEXT NOT NULL,
    assistant_output TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_consolidation_jobs_status
    ON consolidation_jobs (status);
"""


async def ensure_schema(conn: aiosqlite.Connection) -> None:
    """幂等建表，可重复调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
