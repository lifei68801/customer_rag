from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_personas (
    tenant_id  TEXT PRIMARY KEY,
    avatar     TEXT NOT NULL DEFAULT '',
    tagline    TEXT NOT NULL DEFAULT '',
    questions  TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
# 数字人的脸：前台右栏和欢迎语要用的那几样，外加引导问题（questions 列）。
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


async def set_questions(
    conn: aiosqlite.Connection, *, tenant_id: str, questions: list[str]
) -> None:
    """写引导问题。存 JSON 数组而不是另开一张行表：它是一个有序的短列表，
    整体读整体写，拆成行表只会让「顺序」需要一个额外的列来维护。

    avatar/tagline 不在 DO UPDATE 的列里——跟 upsert_persona 反过来对称：
    upsert_persona 写脸时不碰 questions，这里写 questions 时也不碰脸，
    两个编辑动作互不清空对方。

    这里不做校验——校验在路由层（保存时要把「哪几条不通过」告诉用户，
    而这个函数只能返回成功或抛异常，说不出是哪几条）。
    """
    await conn.execute(
        "INSERT INTO tenant_personas (tenant_id, questions) VALUES (?, ?) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "questions = excluded.questions, updated_at = datetime('now')",
        (tenant_id, json.dumps(questions, ensure_ascii=False)),
    )
    await conn.commit()


async def get_questions(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    """读引导问题。没配过时返回空列表。

    自己设 row_factory，理由同 get_persona/get_personas：不自设的话，一旦
    调用顺序被打破（生产路径上依赖 seed_admin_user → get_admin_user 先把
    进程内单例连接的 row_factory 设成 aiosqlite.Row 这个"碰巧"），
    `row["questions"]` 会以 TypeError 收场，而不是取到错的数据。

    JSON 解析失败时也返回空列表并告警：这一列是人写进去的，历史上手工改库
    留下一个坏值是可能的，而它不该让整个前台首屏 500。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT questions FROM tenant_personas WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return []
    try:
        parsed = json.loads(row["questions"])
    except (TypeError, ValueError):
        logger.warning("租户 %r 的 questions 列不是合法 JSON，按「没配」处理", tenant_id)
        return []
    return [q for q in parsed if isinstance(q, str)] if isinstance(parsed, list) else []
