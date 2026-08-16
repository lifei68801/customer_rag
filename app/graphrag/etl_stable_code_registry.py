from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etl_stable_code_registry (
    tenant_id    TEXT NOT NULL,
    scope        TEXT NOT NULL,
    raw_value    TEXT NOT NULL,
    stable_code  TEXT NOT NULL,
    allocated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, scope, raw_value)
);
"""


async def ensure_stable_code_registry_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def allocate_stable_code(
    conn: aiosqlite.Connection, *, tenant_id: str, scope: str, raw_value: str
) -> str:
    """给定 (tenant_id, scope, raw_value)，命中已有分配就复用，未命中就在该
    scope 下分配一个新的五位数序号（从 "00001" 开始）。假设同一租户的 ETL
    任务串行执行，查询命中判断与插入之间没有加锁——见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 3.3 节
    的并发假设说明。
    """
    cursor = await conn.execute(
        "SELECT stable_code FROM etl_stable_code_registry "
        "WHERE tenant_id = ? AND scope = ? AND raw_value = ?",
        (tenant_id, scope, raw_value),
    )
    row = await cursor.fetchone()
    if row is not None:
        return row[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM etl_stable_code_registry WHERE tenant_id = ? AND scope = ?",
        (tenant_id, scope),
    )
    count = (await cursor.fetchone())[0]
    stable_code = f"{count + 1:05d}"
    await conn.execute(
        "INSERT INTO etl_stable_code_registry (tenant_id, scope, raw_value, stable_code, allocated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (tenant_id, scope, raw_value, stable_code),
    )
    await conn.commit()
    return stable_code


async def lookup_stable_code(
    conn: aiosqlite.Connection, *, tenant_id: str, scope: str, raw_value: str
) -> str | None:
    """只读查找 (tenant_id, scope, raw_value) 已分配的稳定码，未命中返回 None、
    不分配新码——供关系写入路径按只读语义解析关系端点的 node_key，避免给
    尚未真实存在（或已被跳过）的实体值意外分配一个新码、进而 MERGE 出一个
    没有 standard_name 属性的幽灵节点。"""
    cursor = await conn.execute(
        "SELECT stable_code FROM etl_stable_code_registry "
        "WHERE tenant_id = ? AND scope = ? AND raw_value = ?",
        (tenant_id, scope, raw_value),
    )
    row = await cursor.fetchone()
    return row[0] if row is not None else None
