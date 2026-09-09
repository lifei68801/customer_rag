from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS attribute_conflicts (
    conflict_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL,
    node_key        TEXT NOT NULL,
    field           TEXT NOT NULL,
    kept_value      TEXT NOT NULL,
    kept_source     TEXT NOT NULL,
    incoming_value  TEXT NOT NULL,
    incoming_source TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'resolved')),
    resolved_value  TEXT,
    resolved_by     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_attribute_conflicts_pending
    ON attribute_conflicts (tenant_id, status, conflict_id);
-- 同一个 (租户, 实体, 属性) 最多只能有**一条待处理**的冲突。
--
-- 部分唯一索引而不是普通唯一索引：已决议的行必须能跟一条新的待处理行共存。
-- 判据里少了 status 的话，那条已决议的行会把新冲突挡在门外——ETL 明天再跑
-- 出一个不同的值，谁也不会知道。
CREATE UNIQUE INDEX IF NOT EXISTS idx_attribute_conflicts_one_pending_per_field
    ON attribute_conflicts (tenant_id, node_key, field)
    WHERE status = 'pending';
"""


class ConflictNotFoundError(Exception):
    """这个租户下没有这个 conflict_id。"""


class ConflictAlreadyResolvedError(Exception):
    """这条冲突已经有人决议过了。"""


async def ensure_attribute_conflicts_schema(conn: aiosqlite.Connection) -> None:
    """属性值冲突表。

    表格 A 说售价 39、表格 B 说 45——改这张表出现之前，后跑的赢，没有任何人
    知道发生过冲突。这是 spec §1 点名的「今天最大的静默失败」。

    处理原则是 Global Constraint 13：**记下来、保留先写的、等人来定**。
    不是「猜一个」，也不是「后写的赢」——那只是把静默覆盖换了个方向。
    """
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def record_conflict(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    field: str,
    kept_value: str,
    kept_source: str,
    incoming_value: str,
    incoming_source: str,
) -> None:
    """记一条冲突。同一个 (实体, 属性) 已有待处理的那条时**更新**它，不新增。

    ETL 每天跑一次，不去重的话审核员面对的是同一个问题的一百个副本。

    更新的是 `incoming_value`/`incoming_source`：第三张表说 52 的话，审核员
    该看到的是「39（商品表）对 52（第三张表）」——留着 45 的话他在对一个
    已经没人主张的值做决定。

    `kept_value` 不动。它是先写进库的那个值，谁也没决定要换掉它；跟着新的
    导入一起变的话，这张表就从「谁跟谁冲突」退化成「最后两次导入是什么」。
    """
    await conn.execute(
        "INSERT INTO attribute_conflicts "
        "(tenant_id, node_key, field, kept_value, kept_source, incoming_value, incoming_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        # 冲突目标必须跟那条部分唯一索引完全一致（含 WHERE），否则 SQLite
        # 匹配不到它，DO UPDATE 不生效、直接抛 UNIQUE 约束错误。
        "ON CONFLICT (tenant_id, node_key, field) WHERE status = 'pending' DO UPDATE SET "
        "incoming_value = excluded.incoming_value, "
        "incoming_source = excluded.incoming_source",
        (tenant_id, node_key, field, kept_value, kept_source, incoming_value, incoming_source),
    )
    await conn.commit()


async def list_conflicts(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    status: str = "pending",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """默认只列待处理的。

    已决议的混在里面的话，审核员会重复处理——他看不出哪些已经定过了。
    要看历史时显式传 `status="resolved"`。

    limit=None 用 -1 承载「不限制」，同 review_queue.list_pending_reviews。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT conflict_id, tenant_id, node_key, field, kept_value, kept_source, "
        "incoming_value, incoming_source, status, resolved_value, resolved_by, "
        "created_at, resolved_at FROM attribute_conflicts "
        "WHERE tenant_id = ? AND status = ? ORDER BY conflict_id LIMIT ? OFFSET ?",
        (tenant_id, status, limit if limit is not None else -1, offset),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def count_conflicts(conn: aiosqlite.Connection, *, tenant_id: str) -> int:
    """待处理的条数。看板和侧边栏的角标用。

    只数 pending：决议之后这个数要减一，不减的话角标永远降不下去——审核员
    处理完一整页那个数字纹丝不动，他会以为自己的操作没生效。
    """
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM attribute_conflicts WHERE tenant_id = ? AND status = 'pending'",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return row[0]


async def resolve_conflict(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    conflict_id: int,
    chosen_value: str,
    resolved_by: str,
) -> str:
    """定下这条冲突用哪个值，返回那个值供调用方写回 terms。

    `chosen_value` **不限于那两个值之一**：两个来源都错是可能的（比如两张表
    都漏了单位），强制二选一等于逼审核员选一个已知是错的。

    `resolved_by` 必填、无默认值：这是一次人工覆盖机器判断的动作，正是最
    需要留痕的那种。

    带租户判据查：只按主键找的话，知道一个 id 就能改别人租户的数据——而 id
    是自增的，猜得到。

    已决议的拒绝再决议：放行的话，第二个人的选择会悄悄覆盖第一个人的，
    而两人都以为自己的生效了。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT status FROM attribute_conflicts WHERE tenant_id = ? AND conflict_id = ?",
        (tenant_id, conflict_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ConflictNotFoundError(f"租户 {tenant_id!r} 下没有冲突 {conflict_id}")
    if row["status"] != "pending":
        raise ConflictAlreadyResolvedError(f"冲突 {conflict_id} 已经处理过了")

    await conn.execute(
        "UPDATE attribute_conflicts SET status = 'resolved', resolved_value = ?, "
        "resolved_by = ?, resolved_at = datetime('now') "
        # WHERE 里再带一次 status='pending'：上面那次读和这次写之间隔着一个
        # 窗口，同一条被两个人同时决议时，第二次 UPDATE 会匹配不到行而不是
        # 覆盖掉第一个人的选择。
        "WHERE tenant_id = ? AND conflict_id = ? AND status = 'pending'",
        (chosen_value, resolved_by, tenant_id, conflict_id),
    )
    await conn.commit()
    return chosen_value
