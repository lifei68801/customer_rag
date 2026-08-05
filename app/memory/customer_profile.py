from __future__ import annotations

from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customer_profiles (
    tenant_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    is_vip INTEGER NOT NULL DEFAULT 0,
    feedback_label TEXT NOT NULL DEFAULT 'neutral',
    communication_style TEXT NOT NULL DEFAULT 'formal',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, customer_id)
);
"""

_DEFAULT_PROFILE = {
    "is_vip": False,
    "feedback_label": "neutral",
    "communication_style": "formal",
}


async def ensure_customer_profile_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def get_customer_profile(
    conn: aiosqlite.Connection, *, tenant_id: str, customer_id: str
) -> dict[str, Any]:
    """未建档的客户返回默认画像（非VIP/中性反馈/正式语气），不是 None——
    主动性引擎的下游逻辑（频率治理、语气调整）都需要一个可用的画像，
    "没画像"和"中性默认画像"在业务含义上是等价的，不需要额外的空值分支。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT is_vip, feedback_label, communication_style FROM customer_profiles "
        "WHERE tenant_id = ? AND customer_id = ?",
        (tenant_id, customer_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return dict(_DEFAULT_PROFILE)
    profile = dict(row)
    profile["is_vip"] = bool(profile["is_vip"])
    return profile


async def upsert_customer_profile(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    customer_id: str,
    is_vip: bool,
    feedback_label: str,
    communication_style: str,
) -> None:
    await conn.execute(
        "INSERT INTO customer_profiles "
        "(tenant_id, customer_id, is_vip, feedback_label, communication_style, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(tenant_id, customer_id) DO UPDATE SET "
        "is_vip=excluded.is_vip, feedback_label=excluded.feedback_label, "
        "communication_style=excluded.communication_style, updated_at=datetime('now')",
        (tenant_id, customer_id, int(is_vip), feedback_label, communication_style),
    )
    await conn.commit()
