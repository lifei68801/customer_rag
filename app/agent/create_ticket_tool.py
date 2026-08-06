from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    question TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    notified_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tickets_pending
    ON tickets (tenant_id, status, notified_at, created_at);
"""


@dataclass(frozen=True)
class CreateTicketResult:
    ticket_id: str
    reason: str


async def ensure_ticket_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def create_ticket(
    *,
    tenant_id: str,
    customer_id: str,
    question: str,
    reason: str,
    conn: aiosqlite.Connection | None = None,
    now: datetime | None = None,
) -> CreateTicketResult:
    """转人工工单的 mock 实现，预留真实工单系统对接接口。

    真实对接（工单系统的具体 API）不在本仓库范围内，需与客服工单
    系统负责方另行排期——见执行计划第5节跨职能依赖清单。

    conn 为可选项：不传则保持纯 mock 行为，不落库；传入则持久化工单
    记录，使 list_stale_pending_tickets() 能找到"挂起过久"的工单——
    这是主动跟进引擎（app/memory/followup_engine.py）目前唯一有真实
    数据支撑的触发信号，见 app/memory/proactive_scan.py。
    """
    ticket_id = str(uuid.uuid4())
    if conn is not None:
        resolved_now = now or datetime.now()
        await ensure_ticket_schema(conn)
        await conn.execute(
            "INSERT INTO tickets "
            "(ticket_id, tenant_id, customer_id, question, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                tenant_id,
                customer_id,
                question,
                reason,
                resolved_now.timestamp(),
            ),
        )
        await conn.commit()
    return CreateTicketResult(ticket_id=ticket_id, reason=reason)


async def list_stale_pending_tickets(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    older_than_seconds: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """找出还是 pending 状态、创建时间早于阈值、且从未跟进过的工单——
    "工单挂起过久"这条主动跟进触发信号的具体判定依据。

    排除已跟进过的工单（notified_at IS NOT NULL），避免同一张工单在
    每次扫描时都被重新评估/重复跟进；是否要发这次跟进则完全交给
    send_followup_if_allowed() 的频率治理决定。
    """
    conn.row_factory = aiosqlite.Row
    threshold = now.timestamp() - older_than_seconds
    cursor = await conn.execute(
        "SELECT * FROM tickets WHERE tenant_id = ? AND status = 'pending' "
        "AND created_at < ? AND notified_at IS NULL ORDER BY created_at",
        (tenant_id, threshold),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_pending_tickets_created_before(
    conn: aiosqlite.Connection, *, tenant_id: str, before: datetime
) -> list[dict[str, Any]]:
    """找出还是 pending 状态、创建时间早于给定时间点的工单——"已知故障
    修复后主动告知"的候选池：只有在修复上线之前提的工单才可能是同一个
    问题，修复之后才提的大概率是别的问题，不参与匹配。

    不排除 notified_at（"挂起过久"触发专用的标记），因为已知修复是完全
    不同的触发原因，去重靠调用方结合 ticket_fix_notifications 表按
    (ticket_id, fix_id) 维度判断，不能复用这个字段。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM tickets WHERE tenant_id = ? AND status = 'pending' "
        "AND created_at < ? ORDER BY created_at",
        (tenant_id, before.timestamp()),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_ticket_notified(
    conn: aiosqlite.Connection, ticket_id: str, *, now: datetime
) -> None:
    await conn.execute(
        "UPDATE tickets SET notified_at = ? WHERE ticket_id = ?",
        (now.timestamp(), ticket_id),
    )
    await conn.commit()
