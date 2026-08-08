from __future__ import annotations

import aiosqlite


async def add_column_if_missing(
    conn: aiosqlite.Connection, *, table: str, column: str, ddl: str
) -> None:
    """幂等地给已存在的表加一列，可重复调用。

    SQLite 没有 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 语法，先用
    `PRAGMA table_info` 查现有列名避免重复 ALTER 报错，不依赖捕获异常
    判断"列已存在"（那样会把真正的 SQL 语法错误也一并吞掉）。
    """
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if column in existing_columns:
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await conn.commit()
