from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_tenants (
    username   TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (username, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_user_tenants_tenant
    ON user_tenants (tenant_id, username);
"""
# 「这个人能访问哪些租户」的唯一事实来源。
#
# admin_users.tenant_id 保留不动，语义从「唯一租户」变成「默认租户」——
# 登录后 current_tenant_id 的初值仍取它。授权判据不再看那一列，只看这张表
# （见 deps.list_accessible_tenant_ids）。
#
# 复合主键就是幂等的实现：重复授权走 INSERT OR IGNORE 落到主键冲突上，
# 不需要先查后插那一圈，也就没有两个请求之间的竞态窗口。


async def ensure_user_tenants_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def grant_tenant_access(
    conn: aiosqlite.Connection, *, username: str, tenant_id: str
) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO user_tenants (username, tenant_id) VALUES (?, ?)",
        (username, tenant_id),
    )
    await conn.commit()


async def revoke_tenant_access(
    conn: aiosqlite.Connection, *, username: str, tenant_id: str
) -> None:
    """撤销一条授权。不存在时是 no-op，不报错——并发下两个管理员同时点
    撤销，第二个人看到 500 会以为撤销失败然后反复重试。"""
    await conn.execute(
        "DELETE FROM user_tenants WHERE username = ? AND tenant_id = ?", (username, tenant_id)
    )
    await conn.commit()


async def list_granted_tenant_ids(conn: aiosqlite.Connection, username: str) -> list[str]:
    cursor = await conn.execute(
        "SELECT tenant_id FROM user_tenants WHERE username = ? ORDER BY tenant_id", (username,)
    )
    return [row["tenant_id"] for row in await cursor.fetchall()]


async def list_usernames_with_access(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    cursor = await conn.execute(
        "SELECT username FROM user_tenants WHERE tenant_id = ? ORDER BY username", (tenant_id,)
    )
    return [row["username"] for row in await cursor.fetchall()]
