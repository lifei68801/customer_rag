from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.tenants_store import TenantNotFoundError

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS organizations (
    org_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
# 组织只是租户之上的一层归拢，不参与任何隔离判据——隔离依旧是 tenant_id
# 一维（见 spec D1）。它存在的唯一理由是让看板能按客户聚合，以及让「一个
# 客户下有哪几个数字人」这个问题有地方回答。


class OrganizationNotFoundError(Exception):
    """指定的 org_id 不在 organizations 表里。"""


class OrganizationAlreadyExistsError(Exception):
    """这个 org_id 已经建过了。"""


async def ensure_organizations_schema(conn: aiosqlite.Connection) -> None:
    """建组织表，并给 tenants 补上 org_id 列。

    org_id 可空且没有外键约束：存量租户不属于任何组织，而这是合法状态，
    不是待修的数据问题。归属关系由 assign_tenant_to_org 在应用层校验——
    SQLite 默认不开启外键强制，写一个不生效的 FOREIGN KEY 会让读代码的人
    以为有约束在兜底。
    """
    await conn.executescript(_SCHEMA_SQL)
    await add_column_if_missing(conn, table="tenants", column="org_id", ddl="TEXT")
    await conn.commit()


async def create_organization(conn: aiosqlite.Connection, *, org_id: str, name: str) -> None:
    cursor = await conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,))
    if await cursor.fetchone() is not None:
        raise OrganizationAlreadyExistsError(f"组织 {org_id!r} 已存在")
    await conn.execute(
        "INSERT INTO organizations (org_id, name) VALUES (?, ?)", (org_id, name)
    )
    await conn.commit()


async def list_organizations(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """自己设 row_factory，跟本仓库其它 store 的做法一致（如
    app/auth/user_tenants_store.py）。生产路径上 review_conn 是进程内单例
    （见 app/api/deps.py::get_review_conn），启动阶段 seed_admin_user →
    get_admin_user 会先把它的 row_factory 设成 aiosqlite.Row，这里目前是
    "碰巧"能按列名取值；不自设的话，`dict(row)` 在未设 row_factory 的连接
    上拿到的是普通元组，会以 ValueError（"dictionary update sequence
    element #0 has length 4; 2 is required"）收场，而不是静默返回错的数据
    ——已用未设 row_factory 的连接验证过。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT org_id, name, status, created_at FROM organizations ORDER BY org_id"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def assign_tenant_to_org(
    conn: aiosqlite.Connection, *, tenant_id: str, org_id: str | None
) -> None:
    """把租户挂到组织下；org_id 传 None 是移出组织。

    两侧都要校验存在性，理由不对称但都成立：

    - 挂到一个不存在的组织下会被拒绝：放行的话这个租户会从组织视图里彻底
      消失——它有 org_id，但那个 org_id 谁也查不到，界面上表现为「这个租户
      不见了」而没有任何报错。
    - tenant_id 不存在时同样要拒绝（`org_id=None` 的移出路径也不例外）：
      `UPDATE ... WHERE tenant_id = ?` 在没有匹配行时不会报错，只是静默
      影响 0 行，调用方会以为分配/移出成功了——这与姊妹函数
      `tenants_store.set_tenant_status` 先查后写、不存在就抛
      `TenantNotFoundError` 是同一种校验，这里补齐，复用同一个异常类型，
      不新造一个同义的，避免调用方要 catch 两种异常。
    """
    if org_id is not None:
        cursor = await conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,))
        if await cursor.fetchone() is None:
            raise OrganizationNotFoundError(f"组织 {org_id!r} 不存在")
    cursor = await conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,))
    if await cursor.fetchone() is None:
        raise TenantNotFoundError(f"租户 {tenant_id!r} 不存在")
    await conn.execute("UPDATE tenants SET org_id = ? WHERE tenant_id = ?", (org_id, tenant_id))
    await conn.commit()


async def list_tenants_in_org(conn: aiosqlite.Connection, org_id: str) -> list[str]:
    """row_factory 的理由同 list_organizations：不自设的话，未设
    row_factory 的连接上 `row["tenant_id"]` 会以 TypeError（"tuple indices
    must be integers or slices, not str"）收场，而不是拿到错的键——已用
    未设 row_factory 的连接验证过。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id FROM tenants WHERE org_id = ? ORDER BY tenant_id", (org_id,)
    )
    return [row["tenant_id"] for row in await cursor.fetchall()]
