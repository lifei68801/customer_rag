from __future__ import annotations

import aiosqlite


async def add_column_if_missing(
    conn: aiosqlite.Connection, *, table: str, column: str, ddl: str
) -> None:
    """幂等地给已存在的表加一列，可重复调用。

    SQLite 没有 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 语法，先用
    `PRAGMA table_info` 查现有列名避免重复 ALTER 报错，不依赖捕获异常
    判断"列已存在"（那样会把真正的 SQL 语法错误也一并吞掉）。

    table/column/ddl 直接拼进 SQL 字符串（SQLite 标识符不能参数化绑定），
    调用方必须传字面量常量，绝不能传任何外部/用户可控的值——这里不做
    校验，靠调用方保证，因为合法的表名/列名/DDL 片段本身就不在一个能用
    正则简单圈定的安全字符集里（DDL 片段含空格、括号、单引号默认值等）。
    """
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if column in existing_columns:
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await conn.commit()
