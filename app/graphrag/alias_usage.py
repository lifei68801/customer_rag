"""审核沉淀出来的别名，以及它们后来被用上了几次。

## 为什么要记

人工审核确认的写法会沉淀成那条 Term 的别名（review_queue._record_alias_from_review），
理由是"一次判定管到以后"。但此前没有任何地方能回答：**那些别名后来真的被
用上了吗？**

别名本身只是 Term 的一列字符串，看不出哪条是审核沉淀的、哪条是建模时手填的，
也看不出它有没有在之后的抽取里帮上忙。没有这个数，"沉淀别名"这件事是不是在
起作用就只能凭感觉——而一个写进去之后从来没被命中过的别名，说明那次沉淀没有
价值，或者抽取那一端根本没走到精确匹配。

## 口径

- **沉淀了几条**：审核批准时写入的别名数。同一条 Term 上同一个写法只算一次。
- **命中了几次**：之后的抽取里，关系的某一端**经由这个别名**（不是标准名）
  对齐到这条 Term 的次数。每条关系的每一端各算一次。

只统计审核沉淀的别名，不统计建模时手填的：看板上这个数要回答的是"审核的
沉淀有没有用"，把手填别名的命中混进来就回答不了。
"""

from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_alias_usage (
    tenant_id   TEXT NOT NULL,
    node_key    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    hit_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, node_key, alias)
);
"""


async def ensure_alias_usage_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def record_review_alias(
    conn: aiosqlite.Connection, *, tenant_id: str, node_key: str, alias: str, created_by: str
) -> None:
    """记一条"这个别名是审核沉淀的"。已经记过就不动（保留原来的命中数）。"""
    await conn.execute(
        "INSERT OR IGNORE INTO review_alias_usage (tenant_id, node_key, alias, created_by) "
        "VALUES (?, ?, ?, ?)",
        (tenant_id, node_key, alias, created_by),
    )
    await conn.commit()


async def record_alias_hit(
    conn: aiosqlite.Connection, *, tenant_id: str, node_key: str, candidate: str
) -> None:
    """抽取时某一端经由别名对齐到了这条 Term：如果那个别名是审核沉淀的，命中数 +1。

    不是审核沉淀的别名（建模时手填的）这里匹配不到任何行，什么都不发生——
    这正是口径要的：只数审核沉淀的那些。

    大小写不敏感，跟 resolve_term 的匹配规则一致：它按小写比对认出了这个
    别名，这里也得按小写找到同一行，否则"对齐成功了却没记上命中"。
    """
    await conn.execute(
        "UPDATE review_alias_usage SET hit_count = hit_count + 1 "
        "WHERE tenant_id = ? AND node_key = ? AND lower(alias) = lower(?)",
        (tenant_id, node_key, candidate.strip()),
    )
    await conn.commit()


async def summarize_review_aliases(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> tuple[int, int]:
    """(沉淀了几条, 一共命中了几次)。"""
    cursor = await conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(hit_count), 0) FROM review_alias_usage WHERE tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return int(row[0]), int(row[1])
