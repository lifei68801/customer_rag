from __future__ import annotations

from datetime import datetime

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS followup_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    sent_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followup_log_customer
    ON followup_log (tenant_id, customer_id, sent_at);
"""


async def ensure_followup_log_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def record_followup_sent(
    conn: aiosqlite.Connection, *, tenant_id: str, customer_id: str, sent_at: datetime
) -> None:
    """记录一次实际发出的主动跟进消息，供后续 delivery_policy.can_send_now()
    做频率治理判断——send_history 需要跨多次 cron/scan 调用持久化，
    不能只是进程内的一次性列表。
    """
    await conn.execute(
        "INSERT INTO followup_log (tenant_id, customer_id, sent_at) VALUES (?, ?, ?)",
        (tenant_id, customer_id, sent_at.timestamp()),
    )
    await conn.commit()


async def get_send_history(
    conn: aiosqlite.Connection, *, tenant_id: str, customer_id: str, since: datetime
) -> list[datetime]:
    cursor = await conn.execute(
        "SELECT sent_at FROM followup_log "
        "WHERE tenant_id = ? AND customer_id = ? AND sent_at >= ? ORDER BY sent_at",
        (tenant_id, customer_id, since.timestamp()),
    )
    rows = await cursor.fetchall()
    return [datetime.fromtimestamp(row[0]) for row in rows]
