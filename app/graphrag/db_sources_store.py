"""数据源表：连接信息 + SQL + 列映射，**没有密码**（spec D3）。

用户配一次列映射要十几分钟，所以这些东西必须存下来可重跑。密码是唯一不存的
那一样——它进了库就意味着：出现在备份里、出现在导出的 sqlite 文件里、出现在
任何一次 `SELECT *` 的日志里。点「重新同步」时现填。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiosqlite

from app.ingestion.db_connector import DbConnectionSpec

#: **这张表没有任何密码列，且不许有。**
#: `tests/graphrag/test_db_sources_store.py::test_the_table_has_no_password_column`
#: 钉着这一点：加一列存密码时那条用例会红。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS db_sources (
    tenant_id      TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    driver         TEXT NOT NULL,
    host           TEXT NOT NULL,
    port           INTEGER NOT NULL,
    database       TEXT NOT NULL,
    username       TEXT NOT NULL,
    query          TEXT NOT NULL,
    mapping        TEXT NOT NULL,
    last_sync_at   TEXT,
    last_sync_rows INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, source_id)
);
"""


async def ensure_db_sources_schema(conn: aiosqlite.Connection) -> None:
    """幂等建表，可重复调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def create_db_source(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    source_id: str,
    name: str,
    spec: DbConnectionSpec,
    query: str,
    mapping: dict[str, Any],
) -> None:
    """存一个数据源。

    收的是 `DbConnectionSpec`（它本身就没有 password 字段）而不是一堆散字段：
    多一层类型，就少一处"顺手把密码也传进来"的机会。
    """
    await conn.execute(
        "INSERT INTO db_sources (tenant_id, source_id, name, driver, host, port,"
        " database, username, query, mapping) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            source_id,
            name,
            spec.driver,
            spec.host,
            spec.port,
            spec.database,
            spec.username,
            query,
            json.dumps(mapping, ensure_ascii=False),
        ),
    )
    await conn.commit()


def _row_to_source(row: aiosqlite.Row) -> dict[str, Any]:
    """行转 dict，顺手把 mapping 解回来。

    在这里解一次而不是让每个调用方自己解：解析代码在三个地方各写一遍的话，
    其中一处迟早忘了 try。
    """
    source = dict(row)
    source["mapping"] = json.loads(source["mapping"])
    return source


async def list_db_sources(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> list[dict[str, Any]]:
    """这个租户的全部数据源，最近建的排前面。

    tenant_id 是查询条件不是断言：拿别的租户的数据过来必须查不到。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM db_sources WHERE tenant_id = ? ORDER BY created_at DESC, source_id",
        (tenant_id,),
    )
    return [_row_to_source(row) for row in await cursor.fetchall()]


async def get_db_source(
    conn: aiosqlite.Connection, *, tenant_id: str, source_id: str
) -> dict[str, Any] | None:
    """取一个数据源，不存在返回 None。"""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM db_sources WHERE tenant_id = ? AND source_id = ?",
        (tenant_id, source_id),
    )
    row = await cursor.fetchone()
    return _row_to_source(row) if row is not None else None


async def delete_db_source(
    conn: aiosqlite.Connection, *, tenant_id: str, source_id: str
) -> None:
    """删一个数据源。别的租户的删不掉。"""
    await conn.execute(
        "DELETE FROM db_sources WHERE tenant_id = ? AND source_id = ?",
        (tenant_id, source_id),
    )
    await conn.commit()


async def touch_last_sync(
    conn: aiosqlite.Connection, *, tenant_id: str, source_id: str, row_count: int
) -> None:
    """记下这次同步的时间和行数。

    **两个一起记**：只有时间的话，用户不知道那次同步是成功导了数据还是导了
    个空。从没同步过时两个字段都是 NULL——编一个 0 或当前时间出来的话，
    列表上会显示「刚刚同步 · 0 行」，而它其实一次都没跑过。

    只更新那一行：漏了 source_id 条件的话，同步一个数据源会让所有数据源都
    显示「刚刚同步」。
    """
    await conn.execute(
        "UPDATE db_sources SET last_sync_at = ?, last_sync_rows = ?"
        " WHERE tenant_id = ? AND source_id = ?",
        (datetime.now().isoformat(timespec="seconds"), row_count, tenant_id, source_id),
    )
    await conn.commit()
