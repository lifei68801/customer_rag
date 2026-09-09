from __future__ import annotations

import aiosqlite

from app.db_migrations import add_column_if_missing

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

-- 问答诊断快照。「答错了」反查实体是发现数据问题的主路径，而当时用了
-- 哪些工具、匹配到哪些实体，此前只活在内存里，一轮对话结束就没了。
CREATE TABLE IF NOT EXISTS qa_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    resolved_question TEXT,
    answer TEXT NOT NULL,
    used_sources TEXT NOT NULL,
    tool_results TEXT NOT NULL,
    -- 这一轮到底怎么收场的：answered / no_match / error。
    -- 少了它，「答不出来」和「答得好」在这张表里长得一模一样。
    -- 默认 answered：加列之前的历史记录没有这个值，而它们当时确实产出了
    -- 答案，读成 no_match 的话报错明细第一屏全是上线前的旧记录。
    outcome TEXT NOT NULL DEFAULT 'answered',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qa_diagnostics_tenant_session
    ON qa_diagnostics (tenant_id, session_id, id);

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

CREATE TABLE IF NOT EXISTS chat_sessions (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user
    ON chat_sessions (tenant_id, user_id, updated_at);

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
    await add_column_if_missing(
        conn, table="memory_history", column="conflict_type", ddl="TEXT",
    )
    # 已经存在的库要补这一列：上面的 CREATE TABLE 是 IF NOT EXISTS，
    # 对老库不生效。
    await add_column_if_missing(
        conn, table="qa_diagnostics", column="outcome",
        ddl="TEXT NOT NULL DEFAULT 'answered'",
    )
