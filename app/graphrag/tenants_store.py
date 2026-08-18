from __future__ import annotations

import aiosqlite

__all__ = [
    "TenantAlreadyExistsError",
    "TenantNotFoundError",
    "create_tenants_table",
    "ensure_tenants_schema",
    "list_tenants",
    "create_tenant",
    "require_active_tenant",
    "set_tenant_status",
]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_VALID_STATUSES = ("active", "disabled")


class TenantNotFoundError(Exception):
    """指定的 tenant_id 不存在于注册表，或存在但状态不是 active。"""


class TenantAlreadyExistsError(Exception):
    """提交的 tenant_id 已经在注册表里。"""


async def _discover_historical_tenant_ids(
    review_conn: aiosqlite.Connection, ingestion_conn: aiosqlite.Connection
) -> set[str]:
    """上线校验前，从两个库里已有的租户作用域表各自 UNION 出历史出现过的
    tenant_id——两个库是不同的 SQLite 文件（review_conn 是
    graph_review_db_path，ingestion_conn 是 ingestion_db_path），aiosqlite
    的两个连接不能直接跨库 UNION，只能各查各的再在 Python 里合并。
    """
    found: set[str] = set()
    for table in ("terms", "ontology_term_types", "etl_runs", "graph_review_queue"):
        cursor = await review_conn.execute(f"SELECT DISTINCT tenant_id FROM {table}")
        rows = await cursor.fetchall()
        found.update(row[0] for row in rows if row[0])
    cursor = await ingestion_conn.execute("SELECT DISTINCT tenant_id FROM ingested_documents")
    rows = await cursor.fetchall()
    found.update(row[0] for row in rows if row[0])
    return found


async def create_tenants_table(conn: aiosqlite.Connection) -> None:
    """只建表，不做迁移回填。真实生产路径不用这个（见 ensure_tenants_schema），
    这个函数是给测试 fixture 用的——很多既有测试的 conn fixture 只建了单张
    表（比如 test_admin_terms_routes.py 的 terms_conn 只建 terms 表），如果
    直接调用 ensure_tenants_schema() 会因为 _discover_historical_tenant_ids()
    要查的 ontology_term_types/etl_runs/graph_review_queue 等表在这个最小
    fixture 里根本不存在而报 "no such table"。
    """
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def ensure_tenants_schema(
    review_conn: aiosqlite.Connection, ingestion_conn: aiosqlite.Connection
) -> None:
    """建表 + 存量数据一次性回填。全程幂等：CREATE TABLE IF NOT EXISTS +
    INSERT OR IGNORE，重复调用（每次进程启动都会走一遍）不会报错也不会
    覆盖已经存在的注册记录（比如后台手动改过的 name/status）。
    """
    await create_tenants_table(review_conn)
    historical_ids = await _discover_historical_tenant_ids(review_conn, ingestion_conn)
    # 全新环境没有任何历史数据时，至少保证 'demo' 存在——这是本仓库其它地方
    # （比如 TenantContext.tsx 的 sessionStorage 默认值）一直假设存在的
    # 兜底租户，注册表上线不能让这个既有默认体验失效。
    if not historical_ids:
        historical_ids.add("demo")
    for tenant_id in historical_ids:
        await review_conn.execute(
            "INSERT OR IGNORE INTO tenants (tenant_id, name, status) VALUES (?, ?, 'active')",
            (tenant_id, tenant_id),
        )
    await review_conn.commit()


async def list_tenants(
    conn: aiosqlite.Connection, *, include_disabled: bool = False
) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    if include_disabled:
        cursor = await conn.execute(
            "SELECT tenant_id, name, status FROM tenants ORDER BY tenant_id"
        )
    else:
        cursor = await conn.execute(
            "SELECT tenant_id, name, status FROM tenants WHERE status = 'active' ORDER BY tenant_id"
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def create_tenant(conn: aiosqlite.Connection, *, tenant_id: str, name: str) -> None:
    cursor = await conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,))
    if await cursor.fetchone() is not None:
        raise TenantAlreadyExistsError(f"租户 {tenant_id!r} 已存在")
    await conn.execute(
        "INSERT INTO tenants (tenant_id, name, status) VALUES (?, ?, 'active')",
        (tenant_id, name),
    )
    await conn.commit()


async def require_active_tenant(conn: aiosqlite.Connection, tenant_id: str) -> None:
    cursor = await conn.execute("SELECT status FROM tenants WHERE tenant_id = ?", (tenant_id,))
    row = await cursor.fetchone()
    if row is None or row[0] != "active":
        raise TenantNotFoundError(f"租户 {tenant_id!r} 不存在或未启用")


async def set_tenant_status(conn: aiosqlite.Connection, tenant_id: str, status: str) -> None:
    if status not in _VALID_STATUSES:
        raise ValueError(f"非法 status: {status!r}")
    cursor = await conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,))
    if await cursor.fetchone() is None:
        raise TenantNotFoundError(f"租户 {tenant_id!r} 不存在")
    await conn.execute("UPDATE tenants SET status = ? WHERE tenant_id = ?", (status, tenant_id))
    await conn.commit()
