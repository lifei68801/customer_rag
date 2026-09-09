"""ETL 跳过的行，跨 run 留档。

此前跳过行只活在**单次 run 的报告**里（`ETLRunReport.skipped_rows`，可下载
CSV）。那份东西随 run 详情页走：用户得先记得是哪一次跑批，才找得到它。
「上周那次导入跳了 127 行」这件事，今天没有任何地方能查——而"哪些行进不来"
恰恰是数据问题最直接的线索。

这张表按租户长期保留，报错明细页的「导入跳过」页签直接读它。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etl_skipped_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    label       TEXT NOT NULL,
    source_file TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_etl_skipped_rows_tenant
    ON etl_skipped_rows (tenant_id, id);
"""


@dataclass(frozen=True)
class SkippedRowRecord:
    """一条跳过行。字段与 `schema_etl.SkippedRow` 一一对应。

    这里不直接复用那个 dataclass：`schema_etl` 反过来 import 本模块（run 结束
    时要写库），复用会成环。
    """

    label: str
    source_file: str
    row_number: int
    reason: str


async def ensure_etl_skipped_rows_schema(conn: aiosqlite.Connection) -> None:
    """幂等建表，可重复调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def record_skipped_rows(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    run_id: str,
    rows: list[SkippedRowRecord],
) -> None:
    """把一次 run 跳过的行整批写进去。

    **先删同 run_id 的旧记录再写**，所以同一次 run 重复调用是替换不是追加：
    追加的话，重试一次导入就让跳过行数翻倍，而用户会以为问题变严重了。
    空批次也要执行这次删除——这一次全过了，上一次那些必须消失，否则用户
    看到的是「上次那 127 行还在」，跑回去改一份根本没问题的表格。

    一次事务写完整批。逐条写库的话两万行的导入会被每行一次事务拖垮，
    所以 `_record_skipped_row` 那边仍然只往报告里攒，不碰数据库。
    """
    await conn.execute(
        "DELETE FROM etl_skipped_rows WHERE tenant_id = ? AND run_id = ?",
        (tenant_id, run_id),
    )
    if rows:
        await conn.executemany(
            "INSERT INTO etl_skipped_rows (tenant_id, run_id, label, source_file,"
            " row_number, reason) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (tenant_id, run_id, row.label, row.source_file, row.row_number, row.reason)
                for row in rows
            ],
        )
    await conn.commit()


async def list_skipped_rows(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列出跳过行，最近的排最前面——入口是「刚才那次跳了什么」。

    tenant_id 是查询条件不是断言：拿别的租户的数据过来必须查不到，不能只靠
    调用方自觉。
    """
    conn.row_factory = aiosqlite.Row
    # SQLite 的 LIMIT 取负数表示不限制，用 -1 承载 limit=None，跟
    # terms_store.list_terms / qa_diagnostics.list_diagnostics 一致。
    cursor = await conn.execute(
        "SELECT id, run_id, label, source_file, row_number, reason, created_at"
        " FROM etl_skipped_rows WHERE tenant_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def count_skipped_rows(conn: aiosqlite.Connection, *, tenant_id: str) -> int:
    """这个租户一共有多少条。分页要用，导航徽标也要用。"""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM etl_skipped_rows WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0
