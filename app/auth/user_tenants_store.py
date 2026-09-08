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
# 这张表是「这个人能访问哪些租户」的唯一事实来源。授权判据已经切过来了：
# app/api/deps.py 的 list_accessible_tenant_ids 读这张表，
# deps.require_tenant_access（租户作用域路由）和切换当前租户那条路由都经由
# deps.assert_tenant_accessible 走它，两处不再各写一遍
# `session.tenant_id != tenant_id`。
#
# admin_users.tenant_id 因此降级成默认值/回退值，不再是「唯一租户」：只有当
# 一个 member 在这张表里一条记录都没有时才顶上（存量账号在这张表刚建时确实
# 一条都没有）。有显式授权时它被完全取代，不是并集——并集的话「撤销某人对
# 他默认租户的访问」永远生效不了。
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
    """这个账号被显式授权的租户，按 tenant_id 正序；没有授权时是空列表。

    自己设 row_factory，跟本仓库其它 store 的做法一致。生产路径上
    require_admin_session 先调 get_admin_user、那个函数把 row_factory 设成了
    aiosqlite.Row，于是这里"碰巧"能按列名取值；靠那个顺序的话，任何一条先
    到达这里的调用路径都会以 TypeError（500）而不是 403 收场。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id FROM user_tenants WHERE username = ? ORDER BY tenant_id", (username,)
    )
    return [row["tenant_id"] for row in await cursor.fetchall()]


async def list_usernames_with_access(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    """被显式授权访问这个租户的账号，按 username 正序。

    row_factory 的理由同 list_granted_tenant_ids：按列名取值的查询自己负责
    设它，不靠调用方碰巧在此之前设过。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT username FROM user_tenants WHERE tenant_id = ? ORDER BY username", (tenant_id,)
    )
    return [row["username"] for row in await cursor.fetchall()]
