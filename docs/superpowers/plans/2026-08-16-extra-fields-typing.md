# extra_fields 类型化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `ontology_term_types.extra_fields`（当前只是字段名白名单）扩展成带类型声明（`string`/`number`/`integer`/`number[]`），并让 `Term.extra_properties` 的值在写入时按声明类型校验，为 MUJI 等 ETL 租户需要的 `numeric_value: number`/`dims: number[]` 这类结构化属性打好基础。

**Architecture:** 存储层不变——`extra_fields`/`extra_properties` 两列本来就是 JSON TEXT，JSON 原生支持数字/数组，Neo4j 的 `SET t += $extra_properties` 参数化写入也本来就支持非字符串类型（详见 spec 第 6 节）。改动全部集中在校验层：`ontology_categories.py` 的字段声明从 `list[str]` 扩展成 `list[ExtraFieldSpec]`，`terms_store.py::_validate_categories` 在"字段名是否在白名单里"之外新增"值是否匹配声明类型"的检查。

**Tech Stack:** Python 3.12、aiosqlite、FastAPI、pytest + pytest-asyncio（`anyio` 标记）。

**Spec:** `docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-design.md`（第 6 节）。

## Global Constraints

- 合法的 `value_type` 取值只有四个：`"string"`、`"number"`、`"integer"`、`"number[]"`。不做枚举/自定义类型/嵌套对象——严格按 spec 第 6 节的范围，不额外扩展。
- `bool` 在 Python 里是 `int` 的子类（`isinstance(True, int)` 为真），类型校验必须显式排除 `bool`，否则 `True`/`False` 会被误判成合法的 `number`/`integer`。
- 字段被从 `term_type` 的声明里移除后，已经写在某条术语记录上的值必须继续保留、不再做类型校验（这是本体基座计划已经定下的"移除字段声明不触碰已有数据"原则，本计划延续这个原则到类型校验层——一个字段不再声明，它的类型自然也不再声明，不需要额外规则）。
- `value_type` 本身的合法性在**声明时**（`create_term_type`/`update_term_type`）校验，不合法直接拒绝；**不要**把这个校验推迟到某条术语真正使用这个字段时才发现。
- 迁移目标：存量 `extra_fields`（旧形态 `["严重等级", "影响范围"]`，纯字符串列表）统一升级成新形态（`[{"name": "严重等级", "value_type": "string"}, ...]`），所有旧字段的 `value_type` 默认赋值为 `"string"`——旧数据从来没有类型信息，`"string"` 是唯一能保证兼容旧数据里已经写过的任意文本值的选择。
- Neo4j 写入路径（`neo4j_client.py::sync_term`、`_SYNC_TERM_QUERY`）**不需要改动**——`SET t += $extra_properties` 已经是参数化 map 写入，Python 的 `int`/`float`/`list` 值原生透传给 Neo4j 驱动，不需要额外的序列化代码。

---

### Task 1: ontology_categories.py — ExtraFieldSpec + 存量数据迁移 + CRUD 改造

**Files:**
- Modify: `app/graphrag/ontology_categories.py`
- Test: `tests/graphrag/test_ontology_categories.py`

**Interfaces:**
- Produces：
  - `ExtraFieldSpec(name: str, value_type: str)` —— frozen dataclass
  - `TermTypeCategory(value: str, extra_fields: list[ExtraFieldSpec], node_key_template: str)` —— `extra_fields` 类型从 `list[str]` 改成 `list[ExtraFieldSpec]`
  - `InvalidExtraFieldTypeError(Exception)` —— `value_type` 不是 `"string"`/`"number"`/`"integer"`/`"number[]"` 之一时抛出
  - `async def create_term_type(conn, tenant_id, *, value: str, extra_fields: list[ExtraFieldSpec] | None = None, node_key_template: str = "") -> None`（`extra_fields` 参数类型变化，其余签名不变）
  - `async def update_term_type(conn, tenant_id, *, value: str, new_value: str, extra_fields: list[ExtraFieldSpec], node_key_template: str) -> None`（同上）

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_ontology_categories.py` 新增：

```python
async def test_create_term_type_with_typed_extra_fields():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="t1", value="错误码",
        extra_fields=[
            ExtraFieldSpec(name="严重等级", value_type="string"),
            ExtraFieldSpec(name="影响范围人数", value_type="integer"),
        ],
    )

    types = await list_term_types(conn, tenant_id="t1")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="严重等级", value_type="string"),
        ExtraFieldSpec(name="影响范围人数", value_type="integer"),
    ]


async def test_create_term_type_rejects_invalid_value_type():
    conn = await _conn()
    with pytest.raises(InvalidExtraFieldTypeError):
        await create_term_type(
            conn, tenant_id="t1", value="错误码",
            extra_fields=[ExtraFieldSpec(name="严重等级", value_type="不存在的类型")],
        )


async def test_update_term_type_with_typed_extra_fields():
    conn = await _conn()
    await create_term_type(conn, tenant_id="t1", value="VariantValue", extra_fields=[])

    await update_term_type(
        conn, tenant_id="t1", value="VariantValue", new_value="VariantValue",
        extra_fields=[
            ExtraFieldSpec(name="numeric_value", value_type="number"),
            ExtraFieldSpec(name="dims", value_type="number[]"),
        ],
        node_key_template="",
    )

    types = await list_term_types(conn, tenant_id="t1")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="numeric_value", value_type="number"),
        ExtraFieldSpec(name="dims", value_type="number[]"),
    ]


async def test_ensure_categories_schema_migrates_legacy_extra_fields_shape():
    """模拟 2026-08-16 之前写入的 extra_fields（纯字符串列表，无类型信息），
    验证迁移把它升级成 [{"name":..., "value_type":"string"}] 形态，旧字段
    统一按 "string" 类型对待（Global Constraints 的迁移规则）。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE ontology_term_types (tenant_id TEXT NOT NULL, value TEXT NOT NULL, "
        "extra_fields TEXT NOT NULL DEFAULT '[]', node_key_template TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (tenant_id, value));"
    )
    await conn.execute(
        "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, node_key_template) "
        "VALUES ('default', '错误码', '[\"严重等级\", \"影响范围\"]', '')"
    )
    await conn.commit()

    await ensure_categories_schema(conn)

    types = await list_term_types(conn, tenant_id="default")
    assert types[0].extra_fields == [
        ExtraFieldSpec(name="严重等级", value_type="string"),
        ExtraFieldSpec(name="影响范围", value_type="string"),
    ]


async def test_ensure_categories_schema_extra_fields_migration_is_idempotent():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="t1", value="错误码",
        extra_fields=[ExtraFieldSpec(name="严重等级", value_type="string")],
    )

    await ensure_categories_schema(conn)
    await ensure_categories_schema(conn)

    types = await list_term_types(conn, tenant_id="t1")
    assert types[0].extra_fields == [ExtraFieldSpec(name="严重等级", value_type="string")]
```

（`_conn()` 是该文件已有的辅助函数，建整套本体 schema；`ExtraFieldSpec`/`InvalidExtraFieldTypeError` 需要在文件顶部的 import 列表里补上。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_categories.py -v -k "typed_extra_fields or invalid_value_type or migrates_legacy_extra_fields or extra_fields_migration_is_idempotent"`
Expected: 全部 FAIL

- [ ] **Step 3: 实现 ExtraFieldSpec、迁移函数、CRUD 改造**

`app/graphrag/ontology_categories.py` 完整替换：

```python
from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    tenant_id         TEXT NOT NULL,
    value             TEXT NOT NULL,
    extra_fields      TEXT NOT NULL DEFAULT '[]',
    node_key_template TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, value)
);
CREATE TABLE IF NOT EXISTS ontology_product_lines (
    value TEXT PRIMARY KEY
);
"""

_VALID_EXTRA_FIELD_VALUE_TYPES = frozenset({"string", "number", "integer", "number[]"})


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
    node_key_template: str


def _validate_extra_field_specs(extra_fields: list[ExtraFieldSpec]) -> None:
    for spec in extra_fields:
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
    """把 2026-08-15 之前的 ontology_term_types 表（value 主键，无
    tenant_id/node_key_template）原地迁移成按租户隔离的新结构，存量数据
    统一归到 tenant_id='default'，node_key_template 留空。幂等，逻辑与
    terms_store.py::_migrate_terms_table_to_tenant_scoped_if_needed 同构。
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
        "INSERT INTO ontology_term_types_new (tenant_id, value, extra_fields, node_key_template) "
        "SELECT 'default', value, extra_fields, '' FROM ontology_term_types"
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
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    await _migrate_extra_fields_value_shape_if_needed(conn)


def _row_to_term_type(row: aiosqlite.Row) -> TermTypeCategory:
    return TermTypeCategory(
        value=row["value"],
        extra_fields=_extra_fields_from_json(row["extra_fields"]),
        node_key_template=row["node_key_template"],
    )


async def list_term_types(conn: aiosqlite.Connection, tenant_id: str) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields, node_key_template FROM ontology_term_types "
        "WHERE tenant_id = ? ORDER BY value",
        (tenant_id,),
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
    node_key_template: str = "",
) -> None:
    extra_fields = extra_fields or []
    _validate_extra_field_specs(extra_fields)
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, node_key_template) "
            "VALUES (?, ?, ?, ?)",
            (tenant_id, value, _extra_fields_to_json(extra_fields), node_key_template),
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
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    new_value: str,
    extra_fields: list[ExtraFieldSpec],
    node_key_template: str,
) -> None:
    """value 是当前名字，new_value 是提交的新名字，允许相同（即不改名）。
    改名时级联更新该租户下 terms 表和 term_type_relation_allowlist 表里
    所有引用旧名字的行，范围收窄到同一租户——term_type 按租户隔离后，
    跨租户级联会误伤其它租户的同名分类。
    """
    _validate_extra_field_specs(extra_fields)
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE tenant_id = ? AND value = ?", (tenant_id, value)
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"分类不存在: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ?, node_key_template = ? "
            "WHERE tenant_id = ? AND value = ?",
            (new_value, _extra_fields_to_json(extra_fields), node_key_template, tenant_id, value),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是已有分类，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE terms SET term_type = ? WHERE tenant_id = ? AND term_type = ?",
            (new_value, tenant_id, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET subject_term_type = ? "
            "WHERE tenant_id = ? AND subject_term_type = ?",
            (new_value, tenant_id, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET object_term_type = ? "
            "WHERE tenant_id = ? AND object_term_type = ?",
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
    """删除保护同样收窄到同一租户范围——见 update_term_type 的说明。"""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE tenant_id = ? AND term_type = ?", (tenant_id, value)
    )
    terms_count = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM term_type_relation_allowlist "
        "WHERE tenant_id = ? AND (subject_term_type = ? OR object_term_type = ?)",
        (tenant_id, value, value),
    )
    allowlist_count = (await cursor.fetchone())[0]
    if terms_count > 0 or allowlist_count > 0:
        raise CategoryInUseError(
            f"分类 {value!r} 仍被 {terms_count} 条术语、{allowlist_count} 条关系约束引用，无法删除"
        )
    await conn.execute(
        "DELETE FROM ontology_term_types WHERE tenant_id = ? AND value = ?", (tenant_id, value)
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_categories.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology_categories.py tests/graphrag/test_ontology_categories.py
git commit -m "feat(graphrag): add typed extra_fields declarations to ontology_term_types"
```

---

### Task 2: terms_store.py — extra_properties 值类型校验

**Files:**
- Modify: `app/graphrag/ontology.py`（`Term.extra_properties` 类型标注）
- Modify: `app/graphrag/terms_store.py`（`_validate_categories`）
- Test: `tests/graphrag/test_terms_store.py`

**Interfaces:**
- Consumes：Task 1 的 `ExtraFieldSpec`、`TermTypeCategory.extra_fields: list[ExtraFieldSpec]`
- Produces：
  - `Term.extra_properties: dict[str, str | int | float | list[float]]`（类型标注放宽，字段本身不变）
  - `InvalidExtraPropertyTypeError(Exception)` —— `terms_store.py` 新异常，`extra_properties` 某个值不匹配其字段声明的 `value_type` 时抛出

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_terms_store.py` 新增：

```python
async def test_create_term_with_typed_extra_properties():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[
            ExtraFieldSpec(name="numeric_value", value_type="number"),
            ExtraFieldSpec(name="dims", value_type="number[]"),
        ],
    )
    await create_product_line(conn, value="示例产品线")

    await create_term(
        conn, tenant_id="t1", standard_name="容量750ml", aliases=[],
        term_type="VariantValue", product_line="示例产品线",
        extra_properties={"numeric_value": 750, "dims": [20.5, 10.0]},
    )

    term = await get_term(conn, tenant_id="t1", standard_name="容量750ml")
    assert term.extra_properties == {"numeric_value": 750, "dims": [20.5, 10.0]}


async def test_create_term_rejects_extra_property_with_wrong_type():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(InvalidExtraPropertyTypeError):
        await create_term(
            conn, tenant_id="t1", standard_name="容量750ml", aliases=[],
            term_type="VariantValue", product_line="示例产品线",
            extra_properties={"numeric_value": "不是数字"},
        )


async def test_create_term_rejects_bool_as_number():
    """bool 是 int 的子类，必须显式排除——见 Global Constraints。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(InvalidExtraPropertyTypeError):
        await create_term(
            conn, tenant_id="t1", standard_name="X", aliases=[],
            term_type="VariantValue", product_line="示例产品线",
            extra_properties={"numeric_value": True},
        )


async def test_update_term_grandfathered_field_skips_type_check():
    """字段被从 term_type 移除后，已写在术语记录上的值不再做类型校验——
    延续本体基座计划"移除字段声明不触碰已有数据"的原则（见 Global
    Constraints）。这里验证：即使移除声明后重新提交同一个值，也不会因为
    "现在没有声明类型、无法判断类型是否匹配"而报错。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import (
        ExtraFieldSpec, create_term_type, create_product_line, update_term_type,
    )
    await create_term_type(
        conn, tenant_id="t1", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, tenant_id="t1", standard_name="X", aliases=[],
        term_type="VariantValue", product_line="示例产品线",
        extra_properties={"numeric_value": 750},
    )
    await update_term_type(
        conn, tenant_id="t1", value="VariantValue", new_value="VariantValue",
        extra_fields=[], node_key_template="",
    )

    # 不应该抛 InvalidExtraPropertyTypeError 或 UnknownCategoryError
    await update_term(
        conn, tenant_id="t1", standard_name="X", new_standard_name="X",
        aliases=[], term_type="VariantValue", product_line="示例产品线",
        extra_properties={"numeric_value": 750},
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v -k "typed_extra_properties or wrong_type or bool_as_number or grandfathered_field"`
Expected: 全部 FAIL

- [ ] **Step 3: 改 Term 类型标注和 _validate_categories**

`app/graphrag/ontology.py`，`Term` dataclass 的 `extra_properties` 字段类型标注：

```python
@dataclass(frozen=True)
class Term:
    tenant_id: str
    node_key: str
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str | int | float | list[float]] = field(default_factory=dict)
```

`app/graphrag/terms_store.py`，新增异常和类型校验函数，改造 `_validate_categories`：

```python
class InvalidExtraPropertyTypeError(Exception):
    """extra_properties 里某个值不匹配该字段在 term_type 上声明的 value_type。"""


def _extra_property_value_matches_type(value: object, value_type: str) -> bool:
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number[]":
        return isinstance(value, list) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        )
    return False


async def _validate_categories(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    product_line: str,
    extra_properties: dict[str, object],
    existing_extra_property_keys: frozenset[str] = frozenset(),
) -> None:
    """product_line 校验保持全局（不受本次改造影响）。term_type 校验
    按租户过滤——每个租户只能使用该租户下注册的分类。

    字段名校验（是否在白名单里）和字段值类型校验（是否匹配声明的
    value_type）是两道独立的检查：existing_extra_property_keys 里的
    "已废弃字段"只豁免字段名检查，不再做类型检查（因为它已经不在
    declared_by_name 里，无法判断"应该是什么类型"）——这是延续本体
    基座计划"移除字段声明不触碰已有数据"的原则，见 Global Constraints。
    """
    types = await list_term_types(conn, tenant_id)
    types_by_value = {t.value: t for t in types}
    if term_type not in types_by_value:
        raise UnknownCategoryError(f"未知分类: {term_type!r}")
    if product_line not in await list_product_lines(conn):
        raise UnknownCategoryError(f"未知产品线: {product_line!r}")
    declared_by_name = {f.name: f for f in types_by_value[term_type].extra_fields}
    declared_fields = set(declared_by_name)
    unknown = set(extra_properties) - declared_fields - existing_extra_property_keys
    if unknown:
        raise UnknownCategoryError(
            f"分类 {term_type!r} 没有声明这些属性字段: {sorted(unknown)}"
        )
    for key, value in extra_properties.items():
        if key not in declared_fields:
            continue
        spec = declared_by_name[key]
        if not _extra_property_value_matches_type(value, spec.value_type):
            raise InvalidExtraPropertyTypeError(
                f"字段 {key!r} 的值 {value!r} 不符合声明的类型 {spec.value_type!r}"
            )
```

`create_term`/`update_term` 的函数签名里 `extra_properties: dict[str, str] | None = None` 改成 `extra_properties: dict[str, object] | None = None`（两处：`create_term` 和 `update_term`），函数体其余部分不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology.py app/graphrag/terms_store.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): validate extra_properties values against declared field types"
```

---

### Task 3: 路由层 — Pydantic 模型改造

**Files:**
- Modify: `app/api/admin_ontology_routes.py`
- Modify: `app/api/admin_terms_routes.py`
- Test: `tests/api/test_admin_ontology_routes.py`
- Test: `tests/api/test_admin_terms_routes.py`

**Interfaces:**
- Consumes：Task 1 的 `ExtraFieldSpec`/`InvalidExtraFieldTypeError`、Task 2 的 `InvalidExtraPropertyTypeError`

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_ontology_routes.py` 新增：

```python
def test_create_term_type_with_typed_extra_fields(client):
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={
            "value": "VariantValue",
            "extra_fields": [
                {"name": "numeric_value", "value_type": "number"},
                {"name": "dims", "value_type": "number[]"},
            ],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert resp.json() == {
        "term_types": [
            {
                "value": "VariantValue",
                "extra_fields": [
                    {"name": "numeric_value", "value_type": "number"},
                    {"name": "dims", "value_type": "number[]"},
                ],
                "node_key_template": "",
            }
        ]
    }


def test_create_term_type_rejects_invalid_extra_field_value_type(client):
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "错误码", "extra_fields": [{"name": "严重等级", "value_type": "不存在的类型"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400
```

在 `tests/api/test_admin_terms_routes.py` 新增（复用文件已有的 `terms_conn`/`_authed_headers`/`SpyGraphClient` fixture）：

```python
def test_create_term_with_typed_extra_properties_returns_200(terms_conn):
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="VariantValue",
            extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )
    )
    asyncio.run(create_product_line(terms_conn, value="p"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "容量750ml", "aliases": [], "term_type": "VariantValue",
                "product_line": "p", "extra_properties": {"numeric_value": 750},
            },
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 200
        assert response.json()["extra_properties"] == {"numeric_value": 750}
    finally:
        app.dependency_overrides.clear()


def test_create_term_rejects_extra_property_wrong_type_returns_400(terms_conn):
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="VariantValue",
            extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )
    )
    asyncio.run(create_product_line(terms_conn, value="p"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "X", "aliases": [], "term_type": "VariantValue",
                "product_line": "p", "extra_properties": {"numeric_value": "不是数字"},
            },
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()
```

（如果该文件顶部尚未 `import asyncio`，需要补上——检查文件现有 import 列表。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_ontology_routes.py tests/api/test_admin_terms_routes.py -v -k "typed_extra_fields or invalid_extra_field_value_type or typed_extra_properties or extra_property_wrong_type"`
Expected: 全部 FAIL

- [ ] **Step 3: 改造两个路由文件**

`app/api/admin_ontology_routes.py`，import 列表加 `ExtraFieldSpec`/`InvalidExtraFieldTypeError`：

```python
from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    ExtraFieldSpec,
    InvalidExtraFieldTypeError,
    create_product_line,
    create_term_type,
    delete_product_line,
    delete_term_type,
    list_product_lines,
    list_term_types,
    update_product_line,
    update_term_type,
)
```

`TermTypeWriteRequest` 和相关路由：

```python
class ExtraFieldSpecRequest(BaseModel):
    name: str
    value_type: str


class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[ExtraFieldSpecRequest] = []
    node_key_template: str = ""


def _to_extra_field_specs(items: list[ExtraFieldSpecRequest]) -> list[ExtraFieldSpec]:
    return [ExtraFieldSpec(name=item.name, value_type=item.value_type) for item in items]


def _extra_field_spec_to_dict(spec: ExtraFieldSpec) -> dict:
    return {"name": spec.name, "value_type": spec.value_type}


@router.get("/{tenant_id}/term-types")
async def list_term_type_categories(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn, tenant_id)
    return {
        "term_types": [
            {
                "value": t.value,
                "extra_fields": [_extra_field_spec_to_dict(f) for f in t.extra_fields],
                "node_key_template": t.node_key_template,
            }
            for t in result
        ]
    }


@router.post("/{tenant_id}/term-types")
async def create_term_type_category(
    tenant_id: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await create_term_type(
            review_conn, tenant_id, value=payload.value,
            extra_fields=_to_extra_field_specs(payload.extra_fields),
            node_key_template=payload.node_key_template,
        )
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.put("/{tenant_id}/term-types/{value}")
async def update_term_type_category(
    tenant_id: str,
    value: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await update_term_type(
            review_conn, tenant_id, value=value, new_value=payload.value,
            extra_fields=_to_extra_field_specs(payload.extra_fields),
            node_key_template=payload.node_key_template,
        )
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="分类不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()
```

（`delete_term_type_category`/product-lines 相关路由**不改动**，原样保留。）

`app/api/admin_terms_routes.py`，import 加 `InvalidExtraPropertyTypeError`：

```python
from app.graphrag.terms_store import (
    InvalidExtraPropertyTypeError,
    TermNameConflictError,
    TermNotFoundError,
    UnknownCategoryError,
    create_term,
    delete_term,
    get_term,
    list_terms,
    update_term,
)
```

`TermResponse`/`TermWriteRequest` 的 `extra_properties` 字段类型放宽：

```python
class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str | int | float | list[float]] = {}


class TermWriteRequest(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str | int | float | list[float]] = {}

    @field_validator("standard_name")
    @classmethod
    def _validate_standard_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("standard_name 不能为空")
        if "/" in stripped:
            raise ValueError("standard_name 不能包含 /")
        return stripped

    @field_validator("term_type", "product_line")
    @classmethod
    def _validate_required_field(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, value: list[str]) -> list[str]:
        return [alias.strip() for alias in value if alias.strip()]
```

`create_new_term`/`update_existing_term` 路由处理函数各自的 `try/except` 块里，在已有的 `except UnknownCategoryError as exc:` 分支后面新增：

```python
    except InvalidExtraPropertyTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

（`create_new_term` 和 `update_existing_term` 两处都要加；`delete_existing_term`/`list_all_terms` 不涉及 `extra_properties` 写入，不用改。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_ontology_routes.py tests/api/test_admin_terms_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 全量回归测试**

Run: `.venv/Scripts/python.exe -u -m pytest -q`
Expected: 除了预先已知的、与本计划无关的 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured` 之外，全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add app/api/admin_ontology_routes.py app/api/admin_terms_routes.py \
  tests/api/test_admin_ontology_routes.py tests/api/test_admin_terms_routes.py
git commit -m "feat(api): expose typed extra_fields/extra_properties through admin routes"
```

---

## Self-Review（写计划人自查，非 subagent 执行）

**Spec 覆盖检查**（对照 `docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-design.md` 第 6 节）：
- `extra_fields` 从字段名白名单扩展成带类型声明 → Task 1 ✅
- `extra_properties` 值按声明类型校验 → Task 2 ✅
- 存储层不需要改动（JSON/Neo4j 参数化写入本就支持非字符串类型）→ 本计划全程未改动 `neo4j_client.py`/`_SYNC_TERM_QUERY`，符合 Global Constraints 明确声明的"不改动"范围 ✅
- 明确不做的条件校验（`value_kind` 决定哪些字段必填）→ 本计划未涉及，与 spec 第 6 节"明确不做"一致 ✅

**占位符扫描**：全文所有代码块均为可直接运行的完整实现/测试，无 TBD/TODO 类占位表述。

**类型一致性检查**：`ExtraFieldSpec(name, value_type)` 在 Task 1 定义后，贯穿 Task 1-3 的 CRUD 函数、校验函数、Pydantic 模型全部一致使用这两个字段名，未出现漂移。`InvalidExtraFieldTypeError`（Task 1，声明时校验）与 `InvalidExtraPropertyTypeError`（Task 2，值提交时校验）是两个语义不同、故意分开的异常类——前者防止业务声明一个不存在的类型，后者防止提交的值跟已声明类型对不上，Task 3 的路由层分别捕获两者并各自映射成 400。
