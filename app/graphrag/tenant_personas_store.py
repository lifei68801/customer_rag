from __future__ import annotations

from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_personas (
    tenant_id  TEXT PRIMARY KEY,
    avatar     TEXT NOT NULL DEFAULT '',
    tagline    TEXT NOT NULL DEFAULT '',
    questions  TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
# 数字人的脸：前台右栏和欢迎语要用的那几样。
#
# questions 这一列本计划只建不用——引导问题连同它的校验逻辑在阶段二
# （见 docs/superpowers/plans/2026-09-08-guided-questions.md）。现在就留出
# 这一列是为了避免阶段二再做一次 ALTER TABLE，不是为了让它先空着。
#
# 一个租户一张脸，所以 tenant_id 直接做主键：数字人就是租户（spec D1），
# 「一个租户两张脸」在这个模型里没有意义。


async def ensure_tenant_personas_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def upsert_persona(
    conn: aiosqlite.Connection, *, tenant_id: str, avatar: str, tagline: str
) -> None:
    """写脸。questions 不在 DO UPDATE 的列里——阶段二会有单独的写入口，
    这里顺手把它清空的话，改一次头像就会把引导问题全删掉。"""
    await conn.execute(
        "INSERT INTO tenant_personas (tenant_id, avatar, tagline) VALUES (?, ?, ?) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "avatar = excluded.avatar, tagline = excluded.tagline, updated_at = datetime('now')",
        (tenant_id, avatar, tagline),
    )
    await conn.commit()


async def get_persona(conn: aiosqlite.Connection, tenant_id: str) -> dict[str, Any] | None:
    """自己设 row_factory，跟本仓库其它 store 的做法一致（如
    app/auth/user_tenants_store.py）。生产路径上 review_conn 是进程内单例
    （见 app/api/deps.py::get_review_conn），启动阶段 seed_admin_user →
    get_admin_user 会先把它的 row_factory 设成 aiosqlite.Row，这里目前是
    "碰巧"能按列名取值；不自设的话，一旦调用顺序被打破，`dict(row)` 会以
    ValueError 收场（已用未设 row_factory 的连接验证过），而不是静默返回
    错的数据。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, avatar, tagline FROM tenant_personas WHERE tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def get_personas(
    conn: aiosqlite.Connection, tenant_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """一次问一批。右栏有 N 个数字人，逐个问就是 N 次查询。

    空列表直接返回、不发查询：拼出来的 `IN ()` 在 SQLite 上是语法错误。

    row_factory 的理由同 get_persona：不自设的话 `row["tenant_id"]` 会以
    TypeError 收场（同样已验证过），而不是拿到错的键。
    """
    conn.row_factory = aiosqlite.Row
    if not tenant_ids:
        return {}
    placeholders = ",".join("?" for _ in tenant_ids)
    cursor = await conn.execute(
        f"SELECT tenant_id, avatar, tagline FROM tenant_personas "
        f"WHERE tenant_id IN ({placeholders})",
        tuple(tenant_ids),
    )
    return {row["tenant_id"]: dict(row) for row in await cursor.fetchall()}
