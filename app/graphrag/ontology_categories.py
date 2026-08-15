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
    term_type 重新 sync_term()。

    同时级联更新 term_type_relation_allowlist 表里所有引用旧名字的
    subject_term_type/object_term_type（跨全部租户、跨 draft/confirmed 两种
    status——分类是全局的，任何租户的任何草稿/已确认约束行都可能引用它）。这张
    表不能从这个模块直接 import（ontology_constraints.py 反过来 import 了本模块，
    import 回去会成环），改用原始 SQL 按表名操作，是这个代码库里已有的跨模块表
    引用方式。"""
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
        await conn.execute(
            "UPDATE terms SET term_type = ? WHERE term_type = ?", (new_value, value)
        )
        # term_type_relation_allowlist 主键是 (tenant_id, subject_term_type,
        # relation_type, object_term_type, status)——重命名理论上可能撞上同一
        # 租户/关系类型/status 下已经存在、改名后变得完全相同的另一行（旧值行和
        # 新值行本来就都在，改名后两者重合）。UPDATE OR IGNORE 逐行忽略这类冲突
        # （幸存的那一行语义上等价，见 ontology_relations.py 既有的 INSERT OR
        # IGNORE 幂等写入模式），不会让整条改名语句因为个别行冲突而整体失败，
        # 未冲突的行照常改名。
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET subject_term_type = ? "
            "WHERE subject_term_type = ?",
            (new_value, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET object_term_type = ? "
            "WHERE object_term_type = ?",
            (new_value, value),
        )
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
        await conn.execute(
            "UPDATE terms SET product_line = ? WHERE product_line = ?", (new_value, value)
        )
    await conn.commit()


async def delete_term_type(conn: aiosqlite.Connection, value: str) -> None:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE term_type = ?", (value,)
    )
    row = await cursor.fetchone()
    terms_count = row[0]
    # term_type_relation_allowlist 里的引用也要算进"在用"——跨全部租户、跨
    # draft/confirmed 两种 status（分类是全局的），否则删除后 add_allowed_
    # combination 的引用校验会让这些行永久无法通过 API 重建（见 update_term_type
    # 的改名级联同款说明）。
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM term_type_relation_allowlist "
        "WHERE subject_term_type = ? OR object_term_type = ?",
        (value, value),
    )
    row = await cursor.fetchone()
    allowlist_count = row[0]
    if terms_count > 0 or allowlist_count > 0:
        raise CategoryInUseError(
            f"分类 {value!r} 仍被 {terms_count} 条术语、{allowlist_count} 条关系约束引用，无法删除"
        )
    await conn.execute("DELETE FROM ontology_term_types WHERE value = ?", (value,))
    await conn.commit()


async def delete_product_line(conn: aiosqlite.Connection, value: str) -> None:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE product_line = ?", (value,)
    )
    row = await cursor.fetchone()
    if row[0] > 0:
        raise CategoryInUseError(f"产品线 {value!r} 仍被 {row[0]} 条术语引用，无法删除")
    await conn.execute("DELETE FROM ontology_product_lines WHERE value = ?", (value,))
    await conn.commit()
