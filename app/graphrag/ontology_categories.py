from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    value        TEXT PRIMARY KEY,
    extra_fields TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS ontology_product_lines (
    value TEXT PRIMARY KEY
);
"""


class CategoryNotFoundError(Exception):
    """指定的分类枚举值不存在。"""


class CategoryInUseError(Exception):
    """删除的分类枚举值仍被 terms 表引用，terms.term_type/product_line 是硬约束外键，
    删除在用的值会让已有术语行结构失效，必须阻止（不同于关系类型删除——那只是写入
    白名单，不是任何表的外键约束对象，见 ontology_relations.py）。"""


class CategoryNameConflictError(Exception):
    """提交的分类值已存在。"""


@dataclass(frozen=True)
class TermTypeCategory:
    value: str
    extra_fields: list[str]


async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def _row_to_term_type(row: aiosqlite.Row) -> TermTypeCategory:
    return TermTypeCategory(value=row["value"], extra_fields=json.loads(row["extra_fields"]))


async def list_term_types(conn: aiosqlite.Connection) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields FROM ontology_term_types ORDER BY value"
    )
    rows = await cursor.fetchall()
    return [_row_to_term_type(row) for row in rows]


async def list_product_lines(conn: aiosqlite.Connection) -> list[str]:
    cursor = await conn.execute("SELECT value FROM ontology_product_lines ORDER BY value")
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def create_term_type(
    conn: aiosqlite.Connection, *, value: str, extra_fields: list[str] | None = None
) -> None:
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types (value, extra_fields) VALUES (?, ?)",
            (value, json.dumps(extra_fields or [], ensure_ascii=False)),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是已有分类，不能重复创建")
    await conn.commit()


async def create_product_line(conn: aiosqlite.Connection, *, value: str) -> None:
    try:
        await conn.execute(
            "INSERT INTO ontology_product_lines (value) VALUES (?)", (value,)
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是已有产品线，不能重复创建")
    await conn.commit()


async def update_term_type(
    conn: aiosqlite.Connection, *, value: str, new_value: str, extra_fields: list[str]
) -> None:
    """value 是当前名字，new_value 是提交的新名字，允许相同（即不改名）。改名时级联
    更新 terms 表里所有引用旧名字的行——term_type 只是 Term 节点上的普通属性（随
    sync_term() 整体覆盖写入），不是节点身份标识，不需要像 standard_name 改名那样
    联动 Neo4j 节点属性更新（rename_term_node），下一次任何该术语的编辑都会用新
    term_type 重新 sync_term()。"""
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE value = ?", (value,)
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"分类不存在: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ? WHERE value = ?",
            (new_value, json.dumps(extra_fields, ensure_ascii=False), value),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是已有分类，不能重复使用")
    if new_value != value:
        try:
            await conn.execute(
                "UPDATE terms SET term_type = ? WHERE term_type = ?", (new_value, value)
            )
        except aiosqlite.OperationalError:
            # terms table doesn't exist yet, which is fine
            pass
    await conn.commit()


async def update_product_line(
    conn: aiosqlite.Connection, *, value: str, new_value: str
) -> None:
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_product_lines WHERE value = ?", (value,)
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"产品线不存在: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_product_lines SET value = ? WHERE value = ?", (new_value, value)
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是已有产品线，不能重复使用")
    if new_value != value:
        try:
            await conn.execute(
                "UPDATE terms SET product_line = ? WHERE product_line = ?", (new_value, value)
            )
        except aiosqlite.OperationalError:
            # terms table doesn't exist yet, which is fine
            pass
    await conn.commit()


async def delete_term_type(conn: aiosqlite.Connection, value: str) -> None:
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM terms WHERE term_type = ?", (value,)
        )
        row = await cursor.fetchone()
        if row[0] > 0:
            raise CategoryInUseError(f"分类 {value!r} 仍被 {row[0]} 条术语引用，无法删除")
    except aiosqlite.OperationalError:
        # terms table doesn't exist yet, which is fine
        pass
    await conn.execute("DELETE FROM ontology_term_types WHERE value = ?", (value,))
    await conn.commit()


async def delete_product_line(conn: aiosqlite.Connection, value: str) -> None:
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM terms WHERE product_line = ?", (value,)
        )
        row = await cursor.fetchone()
        if row[0] > 0:
            raise CategoryInUseError(f"产品线 {value!r} 仍被 {row[0]} 条术语引用，无法删除")
    except aiosqlite.OperationalError:
        # terms table doesn't exist yet, which is fine
        pass
    await conn.execute("DELETE FROM ontology_product_lines WHERE value = ?", (value,))
    await conn.commit()
