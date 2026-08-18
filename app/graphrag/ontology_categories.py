from __future__ import annotations

import json
import re
from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    tenant_id         TEXT NOT NULL,
    value             TEXT NOT NULL,
    extra_fields      TEXT NOT NULL DEFAULT '[]',
    node_key_template TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL,
    PRIMARY KEY (tenant_id, value, status)
);
CREATE TABLE IF NOT EXISTS ontology_product_lines (
    value TEXT PRIMARY KEY
);
"""

_VALID_EXTRA_FIELD_VALUE_TYPES = frozenset({"string", "number", "integer", "number[]"})

_EXTRA_FIELD_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")


class CategoryNotFoundError(Exception):
    """指定的分类枚举值不存在。"""


class CategoryInUseError(Exception):
    """删除的分类枚举值仍被 terms 表引用，terms.term_type/product_line 是硬约束外键，
    删除在用的值会让已有术语行结构失效，必须阻止（不同于关系类型删除——那只是写入
    白名单，不是任何表的外键约束对象，见 ontology_relations.py）。"""


class CategoryNameConflictError(Exception):
    """提交的分类值已存在。"""


class InvalidExtraFieldTypeError(Exception):
    """extra_fields 里某个字段声明的 value_type 不是 "string"/"number"/"integer"/
    "number[]" 之一——在声明时（create_term_type/update_term_type）就拒绝，不推迟到
    某条术语真正提交这个字段的值时才发现（见 Global Constraints）。"""


@dataclass(frozen=True)
class ExtraFieldSpec:
    name: str
    value_type: str


@dataclass(frozen=True)
class TermTypeCategory:
    value: str
    extra_fields: list[ExtraFieldSpec]


def _validate_extra_field_specs(extra_fields: list[ExtraFieldSpec]) -> None:
    for spec in extra_fields:
        if not _EXTRA_FIELD_NAME_PATTERN.match(spec.name):
            raise InvalidExtraFieldTypeError(
                f"字段名 {spec.name!r} 不合法，必须满足 ^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$"
                f"（后续要作为 Neo4j 索引属性名/结构化查询字段名使用，不能含空格或特殊字符）"
            )
        if spec.value_type not in _VALID_EXTRA_FIELD_VALUE_TYPES:
            raise InvalidExtraFieldTypeError(
                f"字段 {spec.name!r} 声明的类型 {spec.value_type!r} 不合法，"
                f"仅支持: {sorted(_VALID_EXTRA_FIELD_VALUE_TYPES)}"
            )


def _extra_fields_to_json(extra_fields: list[ExtraFieldSpec]) -> str:
    return json.dumps(
        [{"name": f.name, "value_type": f.value_type} for f in extra_fields],
        ensure_ascii=False,
    )


def _extra_fields_from_json(raw: str) -> list[ExtraFieldSpec]:
    return [ExtraFieldSpec(name=item["name"], value_type=item["value_type"]) for item in json.loads(raw)]


async def _migrate_term_types_table_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-15 之前的 ontology_term_types 表（value 主键，无 tenant_id）
    原地迁移成按租户隔离的新结构，存量数据统一归到 tenant_id='default'。
    幂等，逻辑与 terms_store.py::_migrate_terms_table_to_tenant_scoped_if_needed
    同构。

    表里仍保留 node_key_template 这一列（NOT NULL DEFAULT ''），但从
    2026-08-18 起应用层代码不再读写它——这个字段最初设计是给 ETL 场景声明
    node_key 拼接模板用的，但实际的 ETL 写入引擎（app/graphrag/schema_etl.py）
    走的是每个租户 ETL 配置里独立声明的 node_key_parts，从不读取这一列，
    它从未真正被消费过。保留列本身只是为了不做一次没有实际收益的
    ALTER TABLE ... DROP COLUMN 迁移，新行都会落一个从未被读取的空字符串。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(ontology_term_types)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "tenant_id" in existing_columns:
        return
    await conn.executescript(
        """
        CREATE TABLE ontology_term_types_new (
            tenant_id         TEXT NOT NULL,
            value             TEXT NOT NULL,
            extra_fields      TEXT NOT NULL DEFAULT '[]',
            node_key_template TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (tenant_id, value)
        );
        """
    )
    await conn.execute(
        "INSERT INTO ontology_term_types_new (tenant_id, value, extra_fields) "
        "SELECT 'default', value, extra_fields FROM ontology_term_types"
    )
    await conn.executescript(
        "DROP TABLE ontology_term_types; "
        "ALTER TABLE ontology_term_types_new RENAME TO ontology_term_types;"
    )
    await conn.commit()


async def _migrate_term_types_add_status_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-19 之前没有 status 列的 ontology_term_types 表（PK 是
    (tenant_id, value)，所有行隐含"直接生效"）迁移成带草稿/已确认两态的
    新结构（PK 变成 (tenant_id, value, status)）。存量行全部落
    status='confirmed'——迁移前它们本来就是"当前生效"的状态，语义上对应
    新模型里的已确认，不是草稿。跟 _migrate_term_types_table_if_needed
    同构，必须排在它之后调用（那个函数先保证 tenant_id 列存在，这个函数
    的 SELECT 依赖 tenant_id 列已经在）。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(ontology_term_types)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "status" in existing_columns:
        return
    await conn.executescript(
        """
        CREATE TABLE ontology_term_types_new (
            tenant_id         TEXT NOT NULL,
            value             TEXT NOT NULL,
            extra_fields      TEXT NOT NULL DEFAULT '[]',
            node_key_template TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL,
            PRIMARY KEY (tenant_id, value, status)
        );
        """
    )
    await conn.execute(
        "INSERT INTO ontology_term_types_new "
        "(tenant_id, value, extra_fields, node_key_template, status) "
        "SELECT tenant_id, value, extra_fields, node_key_template, 'confirmed' "
        "FROM ontology_term_types"
    )
    await conn.executescript(
        "DROP TABLE ontology_term_types; "
        "ALTER TABLE ontology_term_types_new RENAME TO ontology_term_types;"
    )
    await conn.commit()


async def _migrate_extra_fields_value_shape_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-16 之前的 extra_fields 数据（纯字符串列表，如
    '["严重等级", "影响范围"]'）原地升级成带类型声明的形态（如
    '[{"name": "严重等级", "value_type": "string"}, ...]'）。旧字段统一按
    "string" 类型对待（Global Constraints 的迁移规则——旧数据从没有类型
    信息，"string" 是唯一能兼容旧数据里任意已写文本值的选择）。逐行检测：
    JSON 解出来的列表如果第一个元素是 str（而不是 dict），判定为旧形态，
    转换后 UPDATE 回去；空列表或已经是新形态（元素是 dict）的行原样跳过，
    保证幂等。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("SELECT tenant_id, value, extra_fields FROM ontology_term_types")
    rows = await cursor.fetchall()
    for tenant_id, value, extra_fields_raw in rows:
        parsed = json.loads(extra_fields_raw)
        if not parsed or isinstance(parsed[0], dict):
            continue
        migrated = json.dumps(
            [{"name": name, "value_type": "string"} for name in parsed], ensure_ascii=False
        )
        await conn.execute(
            "UPDATE ontology_term_types SET extra_fields = ? WHERE tenant_id = ? AND value = ?",
            (migrated, tenant_id, value),
        )
    await conn.commit()


async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    await _migrate_term_types_table_if_needed(conn)
    await _migrate_term_types_add_status_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    await _migrate_extra_fields_value_shape_if_needed(conn)


def _row_to_term_type(row: aiosqlite.Row) -> TermTypeCategory:
    return TermTypeCategory(
        value=row["value"],
        extra_fields=_extra_fields_from_json(row["extra_fields"]),
    )


async def list_term_types(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields FROM ontology_term_types "
        "WHERE tenant_id = ? AND status = ? ORDER BY value",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [_row_to_term_type(row) for row in rows]


async def list_product_lines(conn: aiosqlite.Connection) -> list[str]:
    cursor = await conn.execute("SELECT value FROM ontology_product_lines ORDER BY value")
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def create_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    extra_fields: list[ExtraFieldSpec] | None = None,
) -> None:
    extra_fields = extra_fields or []
    _validate_extra_field_specs(extra_fields)
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, status) "
            "VALUES (?, ?, ?, 'draft')",
            (tenant_id, value, _extra_fields_to_json(extra_fields)),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是该租户草稿里的分类，不能重复创建")
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
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    new_value: str,
    extra_fields: list[ExtraFieldSpec],
) -> None:
    """value 是草稿里的当前名字，new_value 是提交的新名字，允许相同（即不
    改名）。改名只级联更新该租户草稿约束表（term_type_relation_allowlist）
    里引用旧名字的 draft 行——不再级联更新 terms 表（真实术语只引用已确认
    类型，改草稿定义不影响它们；已确认类型改名后要同步真实术语，用新的
    "迁移实体类型"工具手动触发，见 terms_store.py::migrate_term_type）。
    """
    _validate_extra_field_specs(extra_fields)
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE tenant_id = ? AND value = ? AND status = 'draft'",
        (tenant_id, value),
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"草稿里不存在分类: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ? "
            "WHERE tenant_id = ? AND value = ? AND status = 'draft'",
            (new_value, _extra_fields_to_json(extra_fields), tenant_id, value),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是该租户草稿里的分类，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET subject_term_type = ? "
            "WHERE tenant_id = ? AND subject_term_type = ? AND status = 'draft'",
            (new_value, tenant_id, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET object_term_type = ? "
            "WHERE tenant_id = ? AND object_term_type = ? AND status = 'draft'",
            (new_value, tenant_id, value),
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


async def delete_term_type(conn: aiosqlite.Connection, tenant_id: str, value: str) -> None:
    """terms 表引用检查范围不变（真实术语只引用已确认类型，这个检查天然
    对应"已确认版本是否在用"）；term_type_relation_allowlist 引用检查加
    status='draft'——只拦"删除会破坏当前草稿自洽性"的情况，跟这次删除无关
    的已确认约束不受影响。"""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE tenant_id = ? AND term_type = ?", (tenant_id, value)
    )
    terms_count = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM term_type_relation_allowlist "
        "WHERE tenant_id = ? AND status = 'draft' AND (subject_term_type = ? OR object_term_type = ?)",
        (tenant_id, value, value),
    )
    allowlist_count = (await cursor.fetchone())[0]
    if terms_count > 0 or allowlist_count > 0:
        raise CategoryInUseError(
            f"分类 {value!r} 仍被 {terms_count} 条术语、{allowlist_count} 条关系约束引用，无法删除"
        )
    await conn.execute(
        "DELETE FROM ontology_term_types WHERE tenant_id = ? AND value = ? AND status = 'draft'",
        (tenant_id, value),
    )
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
