# 结构化查询工具重构：数值类型/matched_count 修复 + 统一 graph_query_tool 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `structured_filter_query_tool` 无法对"值即节点"类型（如"销量"）做数值区间过滤的缺陷、修复 `matched_count` 被 `limit` 截断的问题，并把 `graph_query_tool` 的全部能力（别名消歧、邻域展开、任意类型/双向遍历）吸收进 `structured_filter_query_tool`，一次性移除 `graph_query_tool`。

**Architecture:** 本体层给 term type 新增"自身取值类型"声明；查询校验层不再硬编码 `standard_name` 为字符串类型；Cypher 执行层按声明类型做运行时数值转换、真实计数查询、锚点二选一定位（按类型扫描 / 按名字精确解析）、邻居展开。工具/Planner 层用新的 `anchor`/`expand` 参数结构统一暴露这些能力，`graph_query_tool` 整体删除。

**Tech Stack:** FastAPI + aiosqlite（本体存储）、Neo4j（图数据库）、React/TypeScript（管理后台）、pytest（后端测试，`FakeSession`/`FakeDriver`/`FakeGraphClient` 等既有测试替身）。

**Spec:**
- `docs/superpowers/specs/2026-08-24-structured-filter-numeric-value-type-design.md`（Task 1-5）
- `docs/superpowers/specs/2026-08-24-unified-graph-query-tool-design.md`（Task 6-12）

## Global Constraints

- 老数据/老 term type 行为必须保持不变：`standard_name_value_type` 默认值 `"string"`，不需要强制迁移。
- 现有五层白名单校验（`relation_type` 格式+已确认成员、`field`/`target_field` 格式+已确认成员、`operator`-`value_type` 匹配、`term_type` 已确认成员）不能被削弱；`anchor.name` 走 `resolve_term()` 纯 Python 查找，不参与这条校验链，但解析出的 `node_key`/`term_type` 之后涉及的 `constraints`/`expand` 一样要过现有校验。
- `expand.relation_type` 为空时生成的 Cypher 这一跳不能含任何 LLM 可控字符串插值——只省略类型段。
- 不做服务端硬性截断 `limit`（既定风险接受，沿用现状）。
- `graph_query_tool` 一次性移除，不做过渡期并存、不留兼容别名（没有第三方调用方）。
- 每个任务完成后运行改动涉及的测试文件，全绿再进入下一个任务。

---

### Task 1: 本体层——term type 新增"自身取值类型"声明

**Files:**
- Modify: `app/graphrag/ontology_categories.py`
- Test: `tests/graphrag/test_ontology_categories.py`

**Interfaces:**
- Produces: `TermTypeCategory.standard_name_value_type: str = "string"`；`create_term_type`/`update_term_type` 新增关键字参数 `standard_name_value_type: str = "string"`。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/graphrag/test_ontology_categories.py 末尾

async def test_create_term_type_with_standard_name_value_type():
    conn = await _conn()
    await create_term_type(
        conn, tenant_id="default", value="销量", standard_name_value_type="number",
    )

    result = await list_term_types(conn, tenant_id="default", status="draft")

    assert result == [TermTypeCategory(value="销量", extra_fields=[], standard_name_value_type="number")]


async def test_create_term_type_without_standard_name_value_type_defaults_to_string():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="产品")

    result = await list_term_types(conn, tenant_id="default", status="draft")

    assert result[0].standard_name_value_type == "string"


async def test_create_term_type_rejects_invalid_standard_name_value_type():
    conn = await _conn()
    with pytest.raises(InvalidExtraFieldTypeError):
        await create_term_type(
            conn, tenant_id="default", value="销量", standard_name_value_type="number[]",
        )


async def test_update_term_type_changes_standard_name_value_type():
    conn = await _conn()
    await create_term_type(conn, tenant_id="default", value="销量")

    await update_term_type(
        conn, tenant_id="default", value="销量", new_value="销量",
        extra_fields=[], standard_name_value_type="number",
    )

    result = await list_term_types(conn, tenant_id="default", status="draft")
    assert result[0].standard_name_value_type == "number"


async def test_ensure_categories_schema_migrates_legacy_table_without_standard_name_value_type_column():
    """模拟本次改动之前的旧表（没有 standard_name_value_type 列），断言
    ensure_categories_schema 就地加列、老数据全部落默认值 'string'、不影响
    已有字段的读取。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    # 手写建一张"旧形态"的表（没有 standard_name_value_type 列），模拟迁移前状态
    await conn.executescript(
        """
        DROP TABLE ontology_term_types;
        CREATE TABLE ontology_term_types (
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
        "INSERT INTO ontology_term_types (tenant_id, value, status) VALUES ('default', '产品', 'confirmed')"
    )
    await conn.commit()

    await ensure_categories_schema(conn)

    result = await list_term_types(conn, tenant_id="default", status="confirmed")
    assert result == [TermTypeCategory(value="产品", extra_fields=[], standard_name_value_type="string")]


async def test_ensure_categories_schema_add_standard_name_value_type_column_is_idempotent():
    conn = await _conn()
    await ensure_categories_schema(conn)
    await ensure_categories_schema(conn)  # 跑两次不报错
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_categories.py -v`
Expected: 新增的几个测试 FAIL（`TypeError: create_term_type() got an unexpected keyword argument 'standard_name_value_type'` 等）。

- [ ] **Step 3: 实现**

在 `app/graphrag/ontology_categories.py`：

1. `_SCHEMA_SQL` 加一列：

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    tenant_id                 TEXT NOT NULL,
    value                     TEXT NOT NULL,
    extra_fields              TEXT NOT NULL DEFAULT '[]',
    node_key_template         TEXT NOT NULL DEFAULT '',
    standard_name_value_type  TEXT NOT NULL DEFAULT 'string',
    status                    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, value, status)
);
"""
```

2. 新增校验常量（紧跟 `_VALID_EXTRA_FIELD_VALUE_TYPES` 之后）：

```python
_VALID_STANDARD_NAME_VALUE_TYPES = frozenset({"string", "number", "integer"})
```

3. `TermTypeCategory` 加字段：

```python
@dataclass(frozen=True)
class TermTypeCategory:
    value: str
    extra_fields: list[ExtraFieldSpec]
    standard_name_value_type: str = "string"
```

4. 新增校验函数（紧跟 `_validate_extra_field_specs` 之后）：

```python
def _validate_standard_name_value_type(value_type: str) -> None:
    if value_type not in _VALID_STANDARD_NAME_VALUE_TYPES:
        raise InvalidExtraFieldTypeError(
            f"term type 自身取值类型 {value_type!r} 不合法，"
            f"仅支持: {sorted(_VALID_STANDARD_NAME_VALUE_TYPES)}"
        )
```

5. 新增迁移函数（紧跟 `_migrate_extra_fields_value_shape_if_needed` 之后）：

```python
async def _migrate_term_types_add_standard_name_value_type_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(ontology_term_types)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "standard_name_value_type" in existing_columns:
        return
    await conn.execute(
        "ALTER TABLE ontology_term_types "
        "ADD COLUMN standard_name_value_type TEXT NOT NULL DEFAULT 'string'"
    )
    await conn.commit()
```

6. `ensure_categories_schema` 接上这个迁移调用：

```python
async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("DROP TABLE IF EXISTS ontology_product_lines")
    await _migrate_term_types_table_if_needed(conn)
    await _migrate_term_types_add_status_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    await _migrate_extra_fields_value_shape_if_needed(conn)
    await _migrate_term_types_add_standard_name_value_type_if_needed(conn)
```

7. `_row_to_term_type`：

```python
def _row_to_term_type(row: aiosqlite.Row) -> TermTypeCategory:
    return TermTypeCategory(
        value=row["value"],
        extra_fields=_extra_fields_from_json(row["extra_fields"]),
        standard_name_value_type=row["standard_name_value_type"],
    )
```

8. `list_term_types` 的 SELECT 加列：

```python
async def list_term_types(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields, standard_name_value_type FROM ontology_term_types "
        "WHERE tenant_id = ? AND status = ? ORDER BY value",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [_row_to_term_type(row) for row in rows]
```

9. `create_term_type`：

```python
async def create_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    extra_fields: list[ExtraFieldSpec] | None = None,
    standard_name_value_type: str = "string",
) -> None:
    extra_fields = extra_fields or []
    _validate_extra_field_specs(extra_fields)
    _validate_standard_name_value_type(standard_name_value_type)
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types "
            "(tenant_id, value, extra_fields, standard_name_value_type, status) "
            "VALUES (?, ?, ?, ?, 'draft')",
            (tenant_id, value, _extra_fields_to_json(extra_fields), standard_name_value_type),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是该租户草稿里的分类，不能重复创建")
    await conn.commit()
```

10. `update_term_type`：

```python
async def update_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    new_value: str,
    extra_fields: list[ExtraFieldSpec],
    standard_name_value_type: str = "string",
) -> None:
    _validate_extra_field_specs(extra_fields)
    _validate_standard_name_value_type(standard_name_value_type)
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE tenant_id = ? AND value = ? AND status = 'draft'",
        (tenant_id, value),
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"草稿里不存在分类: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ?, standard_name_value_type = ? "
            "WHERE tenant_id = ? AND value = ? AND status = 'draft'",
            (new_value, _extra_fields_to_json(extra_fields), standard_name_value_type, tenant_id, value),
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_categories.py -v`
Expected: 全部 PASS，包括之前所有既有测试（`TermTypeCategory` 的默认值保证旧测试的 `==` 断言不受影响）。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology_categories.py tests/graphrag/test_ontology_categories.py
git commit -m "feat(graphrag): let term types declare their own standard_name value type"
```

---

### Task 2: 管理后台接口——暴露 `standard_name_value_type`

**Files:**
- Modify: `app/api/admin_ontology_routes.py`
- Test: `tests/api/test_admin_ontology_routes.py`

**Interfaces:**
- Consumes: `create_term_type`/`update_term_type` 的 `standard_name_value_type` 关键字参数（Task 1 产出）。
- Produces: `TermTypeWriteRequest.standard_name_value_type: str = "string"`；`GET/POST/PUT /api/admin/{tenant_id}/term-types` 响应体新增该字段。

- [ ] **Step 1: 写失败测试**

先看一眼现有测试文件里创建/更新 term type 的测试怎么写（保持同款断言风格），再追加：

```python
# 追加到 tests/api/test_admin_ontology_routes.py 末尾

async def test_create_term_type_accepts_standard_name_value_type(client, tenant_id):
    response = await client.post(
        f"/api/admin/{tenant_id}/term-types",
        json={"value": "销量", "extra_fields": [], "standard_name_value_type": "number"},
    )
    assert response.status_code == 200
    assert response.json()["standard_name_value_type"] == "number"

    listing = await client.get(f"/api/admin/{tenant_id}/term-types")
    assert listing.json()["term_types"][0]["standard_name_value_type"] == "number"


async def test_create_term_type_without_standard_name_value_type_defaults_to_string(client, tenant_id):
    response = await client.post(
        f"/api/admin/{tenant_id}/term-types",
        json={"value": "产品", "extra_fields": []},
    )
    assert response.json()["standard_name_value_type"] == "string"


async def test_update_term_type_rejects_invalid_standard_name_value_type(client, tenant_id):
    await client.post(f"/api/admin/{tenant_id}/term-types", json={"value": "销量", "extra_fields": []})
    response = await client.put(
        f"/api/admin/{tenant_id}/term-types/销量",
        json={"value": "销量", "extra_fields": [], "standard_name_value_type": "not-a-type"},
    )
    assert response.status_code == 400
```

（`client`/`tenant_id` fixture 名字如果跟文件里现有的不一致，改成实际的 fixture 名——照抄这个文件里其它测试函数的签名。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_ontology_routes.py -v -k standard_name_value_type`
Expected: FAIL（响应体里没有这个字段，或 422 因为 Pydantic 模型不认识这个字段被忽略）。

- [ ] **Step 3: 实现**

在 `app/api/admin_ontology_routes.py`：

```python
class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[ExtraFieldSpecRequest] = []
    standard_name_value_type: str = "string"
```

`create_term_type_category`：

```python
    try:
        await create_term_type(
            review_conn, tenant_id, value=payload.value,
            extra_fields=extra_field_specs,
            standard_name_value_type=payload.standard_name_value_type,
        )
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

`update_term_type_category`：

```python
    try:
        await update_term_type(
            review_conn, tenant_id, value=value, new_value=payload.value,
            extra_fields=extra_field_specs,
            standard_name_value_type=payload.standard_name_value_type,
        )
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="分类不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

`list_term_type_categories` 的响应体拼装：

```python
    return {
        "term_types": [
            {
                "value": t.value,
                "extra_fields": [_extra_field_spec_to_dict(f) for f in t.extra_fields],
                "standard_name_value_type": t.standard_name_value_type,
            }
            for t in result
        ]
    }
```

（`create_term_type_category`/`update_term_type_category` 的返回值是 `payload.model_dump()`，`TermTypeWriteRequest` 加了字段后自动带上，不需要改这两处返回语句本身。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_ontology_routes.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_ontology_routes.py tests/api/test_admin_ontology_routes.py
git commit -m "feat(admin): expose standard_name_value_type on term type API"
```

---

### Task 3: 查询校验层——`standard_name` 不再硬编码为字符串类型

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Consumes: `TermTypeCategory.standard_name_value_type`（Task 1 产出）。
- Produces: `_resolve_field_value_type` 对保留字段 `standard_name` 返回该 term type 声明的类型，不再恒为 `"string"`。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/graphrag/test_structured_filter_query.py 末尾

_SALES_SCHEMA_NUMBER = TermTypeCategory(
    value="销量", extra_fields=[], standard_name_value_type="number",
)


def test_validate_accepts_numeric_operator_on_standard_name_when_declared_number():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "销量",
        "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "gt", "value": 50}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(), term_type_schema={"销量": _SALES_SCHEMA_NUMBER},
    )  # 不抛异常即通过


def test_validate_still_rejects_numeric_operator_on_standard_name_when_default_string():
    """默认 value_type='string' 的 term type，行为不能变——防回归。"""
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "gt", "value": 50}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_relation_target_field_standard_name_respects_declared_type():
    """target_field=standard_name（relation 约束的最后一跳）也要读同一份声明，
    不只是 attribute 约束的 anchor 自身。"""
    args = parse_structured_filter_query_args({
        "anchor_term_type": "订单号",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "BELONG_TO", "direction": "incoming", "target_term_type": "销量"}],
            "target_field": "standard_name", "target_operator": "gt", "target_value": 50,
        }],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types={"BELONG_TO"},
        term_type_schema={
            "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
            "销量": _SALES_SCHEMA_NUMBER,
        },
    )  # 不抛异常即通过
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v -k "standard_name_when_declared or relation_target_field"`
Expected: `test_validate_accepts_numeric_operator_on_standard_name_when_declared_number` 和 `test_validate_relation_target_field_standard_name_respects_declared_type` FAIL（当前硬编码返回 `"string"`，`gt` 不在 `_STRING_OPERATORS` 里，抛出 `StructuredFilterQueryError`）。

- [ ] **Step 3: 实现**

在 `app/graphrag/structured_filter_query.py`，把：

```python
def _resolve_field_value_type(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str:
    if field == _RESERVED_FIELD_NAME:
        return "string"
    category = term_type_schema.get(term_type)
    if category is None:
        raise StructuredFilterQueryError(
            f"term_type {term_type!r} 不在已确认 schema 里，"
            f"可用的 term_type: {sorted(term_type_schema.keys())}"
        )
    for spec in category.extra_fields:
```

改成：

```python
def _resolve_field_value_type(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str:
    category = term_type_schema.get(term_type)
    if category is None:
        raise StructuredFilterQueryError(
            f"term_type {term_type!r} 不在已确认 schema 里，"
            f"可用的 term_type: {sorted(term_type_schema.keys())}"
        )
    if field == _RESERVED_FIELD_NAME:
        return category.standard_name_value_type
    for spec in category.extra_fields:
```

（函数体其余部分——`for spec in category.extra_fields:` 之后的循环、`available_fields`/最终 `raise` ——原样不动。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v`
Expected: 全部 PASS，包括 `test_validate_accepts_standard_name_as_reserved_field`（默认 `_SKU_SCHEMA` 没声明 `standard_name_value_type`，走 dataclass 默认值 `"string"`，`starts_with` 依然通过）。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "fix(graphrag): stop hardcoding standard_name to string, read declared value type"
```

---

### Task 4: 执行层——数值类型转换 + 真实 `matched_count`

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `app/graphrag/structured_filter_query.py`（`run_structured_filter_query` 组装最终结果的部分）
- Test: `tests/graphrag/test_neo4j_client.py`, `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Consumes: `TermTypeCategory.standard_name_value_type`（Task 1）。
- Produces: `execute_structured_filter_query(args, *, tenant_id, term_type_schema)`（新增 `term_type_schema` 参数）；非 `group_by` 分支返回 `{"rows": [...], "total_count": int}` 而不是裸 `list`；`run_structured_filter_query` 的最终结果加 `truncated` 字段，`matched_count` 用 `total_count`。

这一步之后 `execute_structured_filter_query` 还是用 `anchor_term_type` 定位锚点（Task 8 才引入 `resolved`/`NameAnchor` 二选一定位），这里只改数值转换和计数逻辑。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/graphrag/test_neo4j_client.py 末尾

from app.graphrag.ontology_categories import TermTypeCategory


async def test_execute_structured_filter_query_casts_numeric_standard_name_comparison():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="销量",
        constraints=[AttributeConstraint(field="standard_name", operator="gt", value=50)],
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, tenant_id="demo",
        term_type_schema={"销量": TermTypeCategory(value="销量", extra_fields=[], standard_name_value_type="number")},
    )

    assert "toFloat(anchor.standard_name)" in session.last_query


async def test_execute_structured_filter_query_does_not_cast_string_standard_name_comparison():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="SKU",
        constraints=[AttributeConstraint(field="standard_name", operator="starts_with", value="圆角")],
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, tenant_id="demo",
        term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert "toFloat(" not in session.last_query
    assert "toInteger(" not in session.last_query


async def test_execute_structured_filter_query_does_not_cast_extra_field_comparison():
    """extra_fields 数值属性在 Neo4j 里本来就是按声明类型写入的，不需要运行时转换——
    只有 standard_name（节点自身的名字/取值，物理上恒为字符串）才需要。"""
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="SKU",
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, tenant_id="demo",
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )},
    )

    assert "toFloat(" not in session.last_query


async def test_execute_structured_filter_query_returns_real_total_count_beyond_limit():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    # FakeSession 现在需要按调用顺序返回不同结果——第一次调用（计数查询）返回
    # total，第二次调用（取行查询）返回受 limit 截断的行。见下面对 FakeSession 的改动
    # （call_results 是新增的可选参数，按调用顺序消费，跟现有大多数测试用的
    # rows= 参数是两种独立的构造方式，不是同一个参数改了名字）。
    session = FakeSession(call_results=[{"total": 42}, [
        {"standard_name": f"SKU {i}", "node_key": f"SKU:{i}", "term_type": "SKU", "all_properties": {}}
        for i in range(2)
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="SKU",
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        group_by=None, limit=2,
    )

    result = await client.execute_structured_filter_query(
        args, tenant_id="demo", term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert result["total_count"] == 42
    assert len(result["rows"]) == 2
```

`FakeSession` 目前对每次 `.run()` 都返回同一份 `self._rows`——最后一个测试需要区分"计数查询"和"取行查询"两次不同调用返回不同数据。改 `FakeSession`（在 `tests/graphrag/test_neo4j_client.py` 顶部）：

```python
class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def data(self) -> list[dict]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[dict] | None = None, *, call_results: list | None = None) -> None:
        """rows：不管调几次 .run()，每次都返回这同一份数据（绝大多数现有测试的用法，
        不用改）。call_results：按 .run() 调用顺序消费的结果列表，每个元素是
        list[dict]（多行）或 dict（单行，会被包成 [dict]）——两个参数二选一。"""
        self._rows = rows if rows is not None else []
        self._call_results = call_results
        self._call_index = 0
        self.last_query: str | None = None
        self.last_parameters: dict | None = None
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, parameters: dict | None = None) -> FakeResult:
        self.last_query = query
        self.last_parameters = parameters
        self.calls.append((query, parameters))
        if self._call_results is not None:
            result = self._call_results[self._call_index]
            self._call_index += 1
            return FakeResult(result if isinstance(result, list) else [result])
        return FakeResult(self._rows)

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v -k "casts_numeric or does_not_cast or real_total_count"`
Expected: FAIL（`execute_structured_filter_query` 还不接受 `term_type_schema` 参数，也不做转换/真实计数）。

- [ ] **Step 3: 实现**

在 `app/graphrag/neo4j_client.py`：

1. 顶部导入区加（跟现有 `_RELATION_TYPE_NAME_PATTERN` 独立定义同款先例，见文件已有注释）：

```python
# 跟 structured_filter_query.py::_RESERVED_FIELD_NAME 保持同一份约定，独立定义
# 不做跨模块导入——原因同文件顶部 _RELATION_TYPE_NAME_PATTERN 的说明。
_RESERVED_FIELD_NAME = "standard_name"
_CAST_BY_VALUE_TYPE = {"number": "toFloat", "integer": "toInteger"}
```

（还要 `from app.graphrag.ontology_categories import TermTypeCategory` 加进导入区，如果还没有的话。）

2. `_comparison_expression` 加 `cast` 参数：

```python
def _comparison_expression(
    *, prop_expr: str, operator: str, param_name: str, cast: str | None = None
) -> str:
    if cast is not None:
        prop_expr = f"{cast}({prop_expr})"
    if operator == "starts_with":
        return f"{prop_expr} STARTS WITH ${param_name}"
    if operator == "all_lte":
        return f"all(x IN {prop_expr} WHERE x <= ${param_name})"
    if operator == "all_gte":
        return f"all(x IN {prop_expr} WHERE x >= ${param_name})"
    if operator == "any_lte":
        return f"any(x IN {prop_expr} WHERE x <= ${param_name})"
    if operator == "any_gte":
        return f"any(x IN {prop_expr} WHERE x >= ${param_name})"
    return f"{prop_expr} {_COMPARISON_OPERATOR_TO_CYPHER[operator]} ${param_name}"
```

3. 新增一个 cast 解析辅助函数（放在 `_comparison_expression` 附近）：

```python
def _resolve_cast(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str | None:
    if field != _RESERVED_FIELD_NAME:
        return None
    category = term_type_schema.get(term_type)
    if category is None:
        return None
    return _CAST_BY_VALUE_TYPE.get(category.standard_name_value_type)
```

4. `execute_structured_filter_query` 签名加 `term_type_schema`，两处 `_comparison_expression` 调用点传 `cast`，并把非 `group_by` 分支改成先跑计数查询再跑取行查询：

```python
    async def execute_structured_filter_query(
        self,
        args: StructuredFilterQueryArgs,
        *,
        tenant_id: str,
        term_type_schema: dict[str, TermTypeCategory],
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """...原有 docstring 不变..."""
        params: dict[str, Any] = {"tenant_id": tenant_id, "anchor_term_type": args.anchor_term_type}
        where_clauses: list[str] = []

        for i, constraint in enumerate(args.constraints):
            if isinstance(constraint, AttributeConstraint):
                value_param = f"value_{i}"
                params[value_param] = constraint.value
                where_clauses.append(
                    _comparison_expression(
                        prop_expr=f"anchor.{constraint.field}", operator=constraint.operator,
                        param_name=value_param,
                        cast=_resolve_cast(
                            term_type=args.anchor_term_type, field=constraint.field,
                            term_type_schema=term_type_schema,
                        ),
                    )
                )
                continue
            if args.group_by is not None and args.group_by.constraint_index == i:
                continue
            match_pattern, hop_params = _build_hop_match_pattern(constraint.hops, prefix=f"c{i}")
            params.update(hop_params)
            target_value_param = f"c{i}_target_value"
            params[target_value_param] = constraint.target_value
            last_var = f"c{i}_hop{len(constraint.hops) - 1}"
            comparison = _comparison_expression(
                prop_expr=f"{last_var}.{constraint.target_field}",
                operator=constraint.target_operator, param_name=target_value_param,
                cast=_resolve_cast(
                    term_type=constraint.hops[-1].target_term_type, field=constraint.target_field,
                    term_type_schema=term_type_schema,
                ),
            )
            where_clauses.append(f"EXISTS {{ {match_pattern} WHERE {comparison} }}")

        where_sql = " AND ".join(where_clauses) if where_clauses else "true"

        if args.group_by is not None:
            group_constraint = args.constraints[args.group_by.constraint_index]
            assert isinstance(group_constraint, RelationConstraint)
            match_pattern, hop_params = _build_hop_match_pattern(
                group_constraint.hops, prefix=f"g{args.group_by.constraint_index}"
            )
            params.update(hop_params)
            last_var = f"g{args.group_by.constraint_index}_hop{len(group_constraint.hops) - 1}"
            query = (
                "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type}) "
                f"{match_pattern} "
                f"WHERE {where_sql} "
                f"RETURN {last_var}.{group_constraint.target_field} AS value, count(DISTINCT anchor) AS count "
                "ORDER BY count DESC"
            )
            async with self._driver.session() as session:
                result = await session.run(query, params)
                rows = await result.data()
            return {"groups": rows}

        count_query = (
            "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type}) "
            f"WHERE {where_sql} "
            "RETURN count(anchor) AS total"
        )
        rows_query = (
            "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type}) "
            f"WHERE {where_sql} "
            "RETURN anchor.standard_name AS standard_name, anchor.node_key AS node_key, "
            "anchor.type AS term_type, "
            "properties(anchor) AS all_properties "
            "ORDER BY anchor.node_key "
            "LIMIT $limit"
        )
        rows_params = {**params, "limit": args.limit}
        async with self._driver.session() as session:
            count_result = await session.run(count_query, params)
            total_count = (await count_result.data())[0]["total"]
            rows_result = await session.run(rows_query, rows_params)
            rows = await rows_result.data()
        return {"rows": rows, "total_count": total_count}
```

（`ORDER BY anchor.node_key` 是本任务顺手加的既有小缺陷修复，见前置 spec 的说明——`LIMIT` 之前没有排序，截断哪些行不确定。）

- [ ] **Step 4: 运行测试确认通过（先只跑 neo4j_client 部分）**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: 新增测试全部 PASS；`test_execute_structured_filter_query_builds_attribute_where_clause` 等既有测试目前会 FAIL（它们断言 `result == [...]` 裸 list，现在返回的是 `{"rows": [...], "total_count": ...}`）——这是预期的、本任务范围内的破坏性变更，紧接着的 Step 5 一并更新这些既有测试。

- [ ] **Step 5: 更新既有 `neo4j_client.py` 测试以适配新返回形状**

把 `tests/graphrag/test_neo4j_client.py` 里所有断言 `execute_structured_filter_query` 返回值的既有测试，从 `assert result == [...]` 改成 `assert result["rows"] == [...]`（`total_count` 不是这些测试关心的重点，不需要额外断言）。具体改动点（函数名 + 改法）：

- `test_execute_structured_filter_query_builds_attribute_where_clause`：调用改成传 `term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])}`；断言从 `assert result == [...]` 改成 `assert result["rows"] == [...]`。
- `test_execute_structured_filter_query_builds_relation_exists_subquery`：同上加 `term_type_schema` 参数（`{"SKU": TermTypeCategory(value="SKU", extra_fields=[]), "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[])}`），不用改断言（这个测试只断言 `session.last_query` 的文本内容，不断言返回值——但 `count_query`/`rows_query` 现在会产生两次 `.run()` 调用，`session.last_query` 会是最后一次（取行查询）的内容，仍然包含要断言的 `EXISTS {`/`-[:HAS_VARIANT]->` 等子串，测试逻辑不受影响）。
- 其余所有调用了 `execute_structured_filter_query` 的既有测试同样加上 `term_type_schema` 参数（内容按测试里用到的 `anchor_term_type`/`target_term_type` 构造，不声明任何 `extra_fields`/`standard_name_value_type` 即可，用默认值）。

- [ ] **Step 6: 更新 `run_structured_filter_query`（`structured_filter_query.py`）适配新返回形状**

`run_structured_filter_query` 目前：

```python
    try:
        result = await graph_client.execute_structured_filter_query(args, tenant_id=tenant_id)
    except Exception as exc:
        return {"error": f"图谱查询执行失败：{exc}"}

    if isinstance(result, dict):
        return result  # group_by 分支已经是 {"groups": [...]}

    return {
        "matched_count": len(result),
        "results": [
            {
                "standard_name": row["standard_name"],
                "node_key": row["node_key"],
                "term_type": row["term_type"],
                "extra_properties": {
                    k: v
                    for k, v in row["all_properties"].items()
                    if k not in _CORE_TERM_FIELDS and k not in _LEGACY_RESIDUAL_NODE_PROPERTIES
                },
            }
            for row in result
        ],
    }
```

改成：

```python
    try:
        result = await graph_client.execute_structured_filter_query(
            args, tenant_id=tenant_id, term_type_schema=term_type_schema,
        )
    except Exception as exc:
        return {"error": f"图谱查询执行失败：{exc}"}

    if "groups" in result:
        return result  # group_by 分支：{"groups": [...]}

    rows = result["rows"]
    total_count = result["total_count"]
    payload: dict[str, Any] = {
        "matched_count": total_count,
        "results": [
            {
                "standard_name": row["standard_name"],
                "node_key": row["node_key"],
                "term_type": row["term_type"],
                "extra_properties": {
                    k: v
                    for k, v in row["all_properties"].items()
                    if k not in _CORE_TERM_FIELDS and k not in _LEGACY_RESIDUAL_NODE_PROPERTIES
                },
            }
            for row in rows
        ],
    }
    if total_count > len(rows):
        payload["truncated"] = True
    return payload
```

`run_structured_filter_query` 的签名本身在这一步不用改（`term_type_schema` 已经是它现有的参数——`validate_structured_filter_query` 已经在用它了，这里只是把它继续往下传给 `execute_structured_filter_query`）。

`_FakeGraphClient`（`tests/graphrag/test_structured_filter_query.py` 里的测试替身）需要同步改造，让它的返回值也符合新形状——`execute_structured_filter_query` 签名加 `term_type_schema` 参数（忽略即可，不需要用到），非 `group_by` 场景返回 `{"rows": self._rows, "total_count": len(self._rows)}` 而不是裸 `self._rows`：

```python
class _FakeGraphClient:
    def __init__(self, *, rows=None, group_result=None, error=None, total_count=None) -> None:
        self._rows = rows if rows is not None else []
        self._group_result = group_result
        self._error = error
        self._total_count = total_count if total_count is not None else len(self._rows)
        self.last_args = None
        self.last_tenant_id = None

    async def execute_structured_filter_query(self, args, *, tenant_id, term_type_schema):
        self.last_args = args
        self.last_tenant_id = tenant_id
        if self._error is not None:
            raise self._error
        if self._group_result is not None:
            return self._group_result
        return {"rows": self._rows, "total_count": self._total_count}
```

同步更新用到 `_FakeGraphClient` 的既有测试断言（`test_run_structured_filter_query_formats_matched_results` 等）——`result["matched_count"]` 现在应该等于 `total_count`（默认等于 `len(rows)`，这些测试的既有断言 `assert result["matched_count"] == 1` 不用改，因为默认 `total_count = len(rows)`）。

新增一个测试验证 `truncated` 语义：

```python
async def test_run_structured_filter_query_marks_truncated_when_total_exceeds_returned_rows():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(
        rows=[{"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
               "all_properties": {"numeric_value": 600}}],
        total_count=42,
    )

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result["matched_count"] == 42
    assert result["truncated"] is True


async def test_run_structured_filter_query_no_truncated_flag_when_total_matches_returned_rows():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
         "all_properties": {"numeric_value": 600}},
    ])

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "truncated" not in result
```

- [ ] **Step 7: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py tests/graphrag/test_structured_filter_query.py -v`
Expected: 全部 PASS。

- [ ] **Step 8: 运行 `tools.py`/`planner.py` 相关测试确认没有间接破坏**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py tests/agent/test_planner.py tests/agent/test_graph_planner.py -v`
Expected: 这一步之后 `structured_filter_query_tool()`（`app/agent/tools.py`）也间接受影响——它调用 `run_structured_filter_query` 时没有传 `term_type_schema` 吗？**检查一下**：现有 `app/agent/tools.py::structured_filter_query_tool()` 已经在透传 `term_type_schema` 参数（这是既有代码，不用改），所以这一步预期全部 PASS，不需要额外改动 `tools.py`。如果发现有 FAIL，说明 `run_structured_filter_query` 调用链上有遗漏的调用点没传 `term_type_schema`，回头检查修复。

- [ ] **Step 9: 提交**

```bash
git add app/graphrag/neo4j_client.py app/graphrag/structured_filter_query.py tests/graphrag/test_neo4j_client.py tests/graphrag/test_structured_filter_query.py
git commit -m "fix(graphrag): cast numeric standard_name comparisons, report true matched_count"
```

---

### Task 5: 管理后台前端——"自身取值类型"下拉框

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`

**Interfaces:**
- Consumes: Task 2 产出的 `standard_name_value_type` 字段（API 响应体）。

- [ ] **Step 1: 实现（前端没有这个组件的既有单测，手动验证代替 TDD）**

`TermType` interface：

```typescript
interface TermType {
  value: string
  extra_fields: ExtraFieldSpec[]
  standard_name_value_type: string
}
```

新增常量（跟 `VALUE_TYPES` 放在一起）：

```typescript
const STANDARD_NAME_VALUE_TYPES = ['string', 'number', 'integer'] as const
```

`emptyTermTypeDraft`：

```typescript
const emptyTermTypeDraft = (): TermType => ({ value: '', extra_fields: [], standard_name_value_type: 'string' })
```

`startEdit`（第434行附近，`setDraft({ ...item, extra_fields: item.extra_fields.map((f) => ({ ...f })) })`）不用改——`standard_name_value_type` 是标量字段，展开赋值 `...item` 已经带上了。

表单里"类型名"输入框和"属性字段"区块之间插入（第647行 `</label>` 之后、第649行 `<div className="flex flex-col gap-2">` 之前）：

```tsx
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            自身取值类型
            <select
              value={draft.standard_name_value_type}
              onChange={(e) => setDraft((prev) => ({ ...prev, standard_name_value_type: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:shadow-soft focus:outline-none ${focusRing}`}
            >
              {STANDARD_NAME_VALUE_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <span className="text-xs font-normal text-ink-soft">
              这个类型的实例本身代表什么类型的值（比如"销量""收入"这类每个取值都是独立节点的类型，
              应该声明成 number，才能用"大于/小于"做区间查询；大多数类型（产品名、公司名…）保持默认的 string 即可）
            </span>
          </label>
```

列表表格（第571-580行附近）"属性字段数"列旁边加一列展示当前值：

```tsx
              <tr className="border-b border-subtle bg-paper text-ink">
                <th className={cellPadding}>类型名</th>
                <th className={cellPadding}>属性字段数</th>
                <th className={cellPadding}>自身取值类型</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
```

```tsx
                  <td className={cellPadding}>{item.value}</td>
                  <td className={cellPadding}>{item.extra_fields.length}</td>
                  <td className={cellPadding}>{item.standard_name_value_type}</td>
```

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 干净，无输出。

- [ ] **Step 3: 手动验证**

启动前端+后端（`scripts/start-backend.ps1`/`scripts/start-frontend.ps1` 或既有的启动方式），打开管理后台的本体 schema 页面，新建一个 term type，确认下拉框可选、保存后列表显示正确的值，编辑已有 term type 也能改这个字段并保存成功。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/admin/OntologySchemaPage.tsx
git commit -m "feat(admin): add standard_name_value_type dropdown to term type form"
```

---

### Task 6: 统一 anchor/expand 类型 + 解析/校验层重写

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Produces: `NameAnchor`、`TypeAnchor`、`ExpandSpec`、`ResolvedAnchor` 四个新 dataclass；`StructuredFilterQueryArgs.anchor: NameAnchor | TypeAnchor`（替换原 `anchor_term_type: str`）；`StructuredFilterQueryArgs.expand: ExpandSpec | None`；`parse_structured_filter_query_args`/`validate_structured_filter_query` 按新结构重写。
- Consumes: 无跨文件依赖，纯本文件内部重构（Task 7/8 会依赖这里的类型）。

这一步只改**解析+校验**这两个纯函数，不改 `run_structured_filter_query`（Task 7）和执行层（Task 8）——`validate_structured_filter_query` 新签名先加 `resolved: ResolvedAnchor` 参数，Task 7 负责在调用处构造并传入。

- [ ] **Step 1: 写失败测试**

**改写范围有一个重要边界，先读清楚再动手**：这一步只改写**直接调用** `parse_structured_filter_query_args`/`validate_structured_filter_query` 这两个纯函数的测试（把 `"anchor_term_type": "SKU"` 改写成 `"anchor": {"term_type": "SKU"}`，`validate_structured_filter_query` 调用处新增 `resolved=ResolvedAnchor(term_type="SKU", node_key=None)` 参数）。**不要碰**任何调用 `run_structured_filter_query`（编排入口函数）的测试——那些测试要等 Task 7 重写 `run_structured_filter_query` 本身之后才能配套改写并通过；这一步就算把它们的输入 dict 形状改成新的 `anchor` 结构，`run_structured_filter_query` 内部还是 Task 4 遗留的旧代码（`validate_structured_filter_query` 调用处没传新增的 `resolved` 参数），会在这一步产生一个未被捕获的 `TypeError`（不是测试断言失败，是测试直接报错崩溃）——这不是"预期的失败"，是不该在这一步引入的额外噪声，所以幅度上明确不做，留给 Task 7 一次性处理（Task 7 Step 1 会把这些 `run_structured_filter_query` 相关测试从 Task 4 遗留的旧形状直接重写成最终形状，中间不需要经过这一步的过渡态）。

具体范围：`test_run_structured_filter_query_returns_error_on_invalid_args`、`test_run_structured_filter_query_returns_error_on_unconfirmed_field`、`test_run_structured_filter_query_formats_matched_results`、`test_run_structured_filter_query_excludes_legacy_product_line_residue_from_extra_properties`、`test_run_structured_filter_query_passes_through_group_by_result`、`test_run_structured_filter_query_returns_error_when_graph_execution_raises`、以及 Task 4 新加的 `test_run_structured_filter_query_marks_truncated_when_total_exceeds_returned_rows`、`test_run_structured_filter_query_no_truncated_flag_when_total_matches_returned_rows`——这几个函数名本次不改动，原样保留（还是 Task 4 提交时的旧 `"anchor_term_type"` 形状），跳过不动。

其余**所有**用到 `"anchor_term_type"` 或 `args.anchor_term_type`、且只涉及 `parse_structured_filter_query_args`/`validate_structured_filter_query` 这两个函数（不经过 `run_structured_filter_query`）的测试（包括 Task 3 新加的那几个），都要做这个改写，逐一过一遍文件，不要漏。改写示例（其余测试照此模式类推）：

```python
def test_parse_attribute_constraint():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    assert args.anchor == TypeAnchor(term_type="SKU")
    assert args.constraints == [AttributeConstraint(field="numeric_value", operator="gt", value=500)]
    assert args.expand is None
    assert args.group_by is None
    assert args.limit == 20


def test_validate_accepts_confirmed_field_and_matching_operator():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )  # 不抛异常即通过
```

再新增覆盖新能力的测试（追加到文件末尾）：

```python
def test_parse_name_anchor():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola", "type_hint": "公司"},
        "constraints": [],
    })
    assert args.anchor == NameAnchor(name="coke-cola", type_hint="公司")


def test_parse_name_anchor_without_type_hint():
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}, "constraints": []})
    assert args.anchor == NameAnchor(name="coke-cola", type_hint=None)


def test_parse_rejects_anchor_with_both_name_and_term_type():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"name": "coke-cola", "term_type": "公司"},
            "constraints": [],
        })


def test_parse_rejects_anchor_with_neither_name_nor_term_type():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {}, "constraints": []})


def test_parse_rejects_missing_anchor():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"constraints": []})


def test_parse_name_anchor_allows_empty_constraints():
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}})
    assert args.constraints == []


def test_parse_type_anchor_rejects_empty_constraints_without_expand():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {"term_type": "SKU"}, "constraints": []})


def test_parse_type_anchor_rejects_empty_constraints_even_with_expand():
    """expand 不是过滤条件的替代品——TypeAnchor 模式下无约束全量扫描依然禁止，
    不因为设了 expand 就放行，见设计文档。"""
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"}, "constraints": [],
            "expand": {"hops": 1},
        })


def test_parse_expand_defaults():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {},
    })
    assert args.expand == ExpandSpec(hops=1, relation_type=None, direction="both")


def test_parse_expand_with_explicit_values():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {"hops": 2, "relation_type": "BELONG_TO", "direction": "outgoing"},
    })
    assert args.expand == ExpandSpec(hops=2, relation_type="BELONG_TO", direction="outgoing")


def test_parse_expand_rejects_invalid_hops():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {"name": "x"}, "expand": {"hops": 3}})


def test_parse_expand_rejects_invalid_direction():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {"name": "x"}, "expand": {"direction": "sideways"}})


def test_parse_no_expand_defaults_to_none():
    args = parse_structured_filter_query_args({"anchor": {"name": "x"}})
    assert args.expand is None


def test_validate_name_anchor_does_not_require_term_type_schema_membership_for_type_hint():
    """type_hint 只是喂给 resolve_term 的消歧提示，不是需要预先确认的 schema 成员——
    resolved.term_type（解析后的真实类型）仍然要过 schema 校验，但 type_hint 本身不需要。"""
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola", "type_hint": "随便什么"}})
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="公司:Coca-Cola"),
        confirmed_relation_types=set(), term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )  # 不抛异常即通过——type_hint="随便什么" 不校验


def test_validate_rejects_resolved_term_type_not_in_schema():
    """防御性检查：resolve_term 解析出的 term_type 理论上应该在已确认 schema 里，
    但仍要检查，不能假定术语表和 schema 天然一致。"""
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}})
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="不存在的类型", node_key="x"),
            confirmed_relation_types=set(), term_type_schema={},
        )


def test_validate_expand_relation_type_must_be_confirmed():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {"relation_type": "NOT_CONFIRMED"},
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="公司", node_key="x"),
            confirmed_relation_types={"BELONG_TO"},
            term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
        )


def test_validate_expand_relation_type_none_skips_confirmed_check():
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}, "expand": {}})
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="x"),
        confirmed_relation_types=set(),
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )  # 不抛异常即通过——relation_type=None 不用查白名单


def test_validate_expand_relation_type_confirmed_passes():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {"relation_type": "BELONG_TO"},
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="x"),
        confirmed_relation_types={"BELONG_TO"},
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )  # 不抛异常即通过
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v`
Expected: 大量 FAIL（`StructuredFilterQueryArgs` 还没有 `anchor`/`expand` 字段，`NameAnchor`/`TypeAnchor`/`ExpandSpec`/`ResolvedAnchor` 还不存在）。

- [ ] **Step 3: 实现**

在 `app/graphrag/structured_filter_query.py`：

1. 新增四个 dataclass（紧跟 `RelationConstraint` 之后、`GroupBy` 之前）：

```python
@dataclass(frozen=True)
class NameAnchor:
    name: str
    type_hint: str | None


@dataclass(frozen=True)
class TypeAnchor:
    term_type: str


@dataclass(frozen=True)
class ExpandSpec:
    hops: int
    relation_type: str | None
    direction: str  # "outgoing" | "incoming" | "both"


@dataclass(frozen=True)
class ResolvedAnchor:
    term_type: str
    node_key: str | None
```

2. `StructuredFilterQueryArgs` 改成：

```python
@dataclass(frozen=True)
class StructuredFilterQueryArgs:
    anchor: NameAnchor | TypeAnchor
    constraints: list[AttributeConstraint | RelationConstraint]
    expand: ExpandSpec | None
    group_by: GroupBy | None
    limit: int
```

3. 新增常量（跟 `_MAX_HOPS`/`_RESERVED_FIELD_NAME` 放一起）：

```python
_VALID_EXPAND_DIRECTIONS = frozenset({"outgoing", "incoming", "both"})
_VALID_EXPAND_HOPS = frozenset({1, 2})
```

4. 新增解析函数：

```python
def _parse_anchor(raw: dict) -> NameAnchor | TypeAnchor:
    if not isinstance(raw, dict):
        raise StructuredFilterQueryError(f"anchor 必须是 dict，收到: {raw!r}")
    has_name = "name" in raw
    has_term_type = "term_type" in raw
    if has_name and has_term_type:
        raise StructuredFilterQueryError("anchor 不能同时提供 name 和 term_type，二选一")
    if has_name:
        return NameAnchor(name=raw["name"], type_hint=raw.get("type_hint"))
    if has_term_type:
        return TypeAnchor(term_type=raw["term_type"])
    raise StructuredFilterQueryError("anchor 必须提供 name 或 term_type 之一")


def _parse_expand(raw: dict | None) -> ExpandSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StructuredFilterQueryError(f"expand 必须是 dict，收到: {raw!r}")
    hops = raw.get("hops", 1)
    if hops not in _VALID_EXPAND_HOPS:
        raise StructuredFilterQueryError(f"expand.hops 必须是 1 或 2，收到: {hops!r}")
    direction = raw.get("direction", "both")
    if direction not in _VALID_EXPAND_DIRECTIONS:
        raise StructuredFilterQueryError(
            f"expand.direction 必须是 {sorted(_VALID_EXPAND_DIRECTIONS)} 之一，收到: {direction!r}"
        )
    relation_type = raw.get("relation_type")
    return ExpandSpec(hops=hops, relation_type=relation_type, direction=direction)
```

5. `parse_structured_filter_query_args` 重写：

```python
def parse_structured_filter_query_args(raw: dict) -> StructuredFilterQueryArgs:
    """把 LLM 工具调用传来的原始 JSON dict 解析成结构化参数——只做形状校验（必填
    字段是否存在、hops 跳数、operator 是否在协议允许的枚举里），不查 schema 是否
    真的已确认，那是 validate_structured_filter_query 的职责（需要 confirmed_
    relation_types/term_type_schema 这两份数据，本函数没有）。"""
    if not isinstance(raw, dict):
        raise StructuredFilterQueryError(f"结构化过滤查询参数必须是 dict，收到: {raw!r}")
    try:
        raw_anchor = raw["anchor"]
    except KeyError as exc:
        raise StructuredFilterQueryError(f"缺少必填字段: {exc}") from exc
    anchor = _parse_anchor(raw_anchor)
    expand = _parse_expand(raw.get("expand"))

    raw_constraints = raw.get("constraints", [])
    if not isinstance(raw_constraints, list):
        raise StructuredFilterQueryError(f"constraints 必须是 list，收到: {raw_constraints!r}")
    if isinstance(anchor, TypeAnchor) and not raw_constraints:
        raise StructuredFilterQueryError(
            "anchor.term_type 模式下 constraints 不能为空，至少提供一个过滤条件"
            "（expand 不能替代过滤条件——不做无约束全量扫描）"
        )
    constraints = [_parse_constraint(c) for c in raw_constraints]
    group_by = _parse_group_by(raw.get("group_by"), constraints=constraints)
    limit = raw.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise StructuredFilterQueryError(f"limit 必须是正整数，收到: {limit!r}")
    return StructuredFilterQueryArgs(
        anchor=anchor, constraints=constraints, expand=expand, group_by=group_by, limit=limit,
    )
```

6. `validate_structured_filter_query` 重写（签名新增 `resolved`，不再自己判断 anchor 是哪种模式）：

```python
def validate_structured_filter_query(
    args: StructuredFilterQueryArgs,
    *,
    resolved: ResolvedAnchor,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> None:
    """schema 层面的校验。resolved 由调用方（run_structured_filter_query）解析
    args.anchor 之后传入——NameAnchor 模式先跑 resolve_term()，TypeAnchor 模式
    直接取 term_type，两种模式统一后这里只关心 resolved.term_type，不再需要
    区分 args.anchor 原始是哪种形态。"""
    if resolved.term_type not in term_type_schema:
        raise StructuredFilterQueryError(
            f"term_type {resolved.term_type!r} 不在已确认 schema 里，"
            f"可用的 term_type: {sorted(term_type_schema.keys())}"
        )
    for constraint in args.constraints:
        if isinstance(constraint, AttributeConstraint):
            value_type = _resolve_field_value_type(
                term_type=resolved.term_type, field=constraint.field, term_type_schema=term_type_schema,
            )
            _validate_operator_for_value_type(field=constraint.field, operator=constraint.operator, value_type=value_type)
            continue
        for hop in constraint.hops:
            if not isinstance(hop.relation_type, str) or not _RELATION_TYPE_NAME_PATTERN.match(hop.relation_type):
                raise StructuredFilterQueryError(f"关系类型名字不合法: {hop.relation_type!r}")
            if hop.relation_type not in confirmed_relation_types:
                raise StructuredFilterQueryError(
                    f"relation_type {hop.relation_type!r} 不在已确认 schema 里，"
                    f"可用的 relation_type: {sorted(confirmed_relation_types)}"
                )
            if not isinstance(hop.target_term_type, str) or hop.target_term_type not in term_type_schema:
                raise StructuredFilterQueryError(
                    f"target_term_type {hop.target_term_type!r} 不在已确认 schema 里，"
                    f"可用的 term_type: {sorted(term_type_schema.keys())}"
                )
        last_hop = constraint.hops[-1]
        value_type = _resolve_field_value_type(
            term_type=last_hop.target_term_type, field=constraint.target_field, term_type_schema=term_type_schema,
        )
        _validate_operator_for_value_type(
            field=constraint.target_field, operator=constraint.target_operator, value_type=value_type,
        )
    if args.expand is not None and args.expand.relation_type is not None:
        if not _RELATION_TYPE_NAME_PATTERN.match(args.expand.relation_type):
            raise StructuredFilterQueryError(f"关系类型名字不合法: {args.expand.relation_type!r}")
        if args.expand.relation_type not in confirmed_relation_types:
            raise StructuredFilterQueryError(
                f"relation_type {args.expand.relation_type!r} 不在已确认 schema 里，"
                f"可用的 relation_type: {sorted(confirmed_relation_types)}"
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v -k "not run_structured_filter_query"`
Expected: 全部 PASS（改写过的旧测试 + 新增测试，不含上面列出的、本任务明确跳过不动的 `run_structured_filter_query` 相关测试）。

不要额外去跑不带 `-k` 过滤的全量 `test_structured_filter_query.py`——那 8 个跳过不动的 `run_structured_filter_query` 测试此刻还是 Task 4 提交时的旧代码，`run_structured_filter_query` 内部调用 `validate_structured_filter_query` 的地方还没传新增的 `resolved` 参数，会抛 `TypeError`（不是预期要看到的输出，是本任务明确不处理、留给 Task 7 的已知空洞，不需要跑出来确认）。`neo4j_client.py` 同理，Task 8 之前不需要能跑通，这一步不用管它。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "refactor(graphrag): introduce anchor/expand types, unify NameAnchor/TypeAnchor parsing"
```

---

### Task 7: `run_structured_filter_query` 编排重写——`resolve_term` 集成 + 新返回结构

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Consumes: `NameAnchor`/`TypeAnchor`/`ResolvedAnchor`（Task 6）；`resolve_term`（`app/graphrag/ontology.py`，已有）。
- Produces: `run_structured_filter_query(raw_args, *, terms, graph_client, tenant_id, confirmed_relation_types, term_type_schema)`（新增 `terms` 参数）；非 `group_by` 返回形状从 `{"matched_count", "results", "truncated"?}` 改成 `{"matched_count", "anchors", "truncated"?}`，`anchors` 每项可能带 `neighbors`。

- [ ] **Step 1: 写失败测试**

先把 `tests/graphrag/test_structured_filter_query.py` 里所有调用 `run_structured_filter_query(...)` 的既有测试，输入的 `"anchor_term_type": "SKU"` 改成 `"anchor": {"term_type": "SKU"}`，调用参数加 `terms=[]`（这些测试都是 `TypeAnchor` 场景，不需要真的解析 name，传空列表即可），断言 `result["results"]` 改成 `result["anchors"]`。逐一改写文件里这几个函数：`test_run_structured_filter_query_returns_error_on_invalid_args`、`test_run_structured_filter_query_returns_error_on_unconfirmed_field`、`test_run_structured_filter_query_formats_matched_results`、`test_run_structured_filter_query_excludes_legacy_product_line_residue_from_extra_properties`、`test_run_structured_filter_query_passes_through_group_by_result`、`test_run_structured_filter_query_returns_error_when_graph_execution_raises`、Task 4 新加的 `test_run_structured_filter_query_marks_truncated_when_total_exceeds_returned_rows`、`test_run_structured_filter_query_no_truncated_flag_when_total_matches_returned_rows`。

改写示例：

```python
async def test_run_structured_filter_query_formats_matched_results():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
         "all_properties": {
             "tenant_id": "muji", "node_key": "SKU:1", "standard_name": "圆角收纳盒 500ml",
             "type": "SKU", "numeric_value": 600,
         }},
    ])

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result["matched_count"] == 1
    assert result["anchors"] == [{
        "standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1",
        "term_type": "SKU",
        "extra_properties": {"numeric_value": 600},
    }]
    assert graph_client.last_tenant_id == "muji"
```

新增测试覆盖 `NameAnchor` 编排逻辑：

```python
from app.graphrag.ontology import Term

_COKE_TERM = Term(
    tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
    aliases=["coke-cola", "可口可乐"], term_type="公司",
)


async def test_run_structured_filter_query_resolves_name_anchor_and_uses_node_key():
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {"anchor": {"name": "coke-cola"}},
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(), term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )

    assert result["matched_count"] == 1
    assert result["anchors"][0]["standard_name"] == "Coca-Cola"
    # 锚点用解析出的 node_key 精确定位，不是按 type 扫描——通过 _FakeGraphClient
    # 记录的 last_resolved 断言 resolve_term() 解析出的 node_key 被正确传下去。
    assert graph_client.last_resolved.node_key == "公司:Coca-Cola"


async def test_run_structured_filter_query_name_anchor_not_resolved_returns_zero_without_querying_graph():
    class _ExplodingGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            raise AssertionError("未命中术语表时不应该查图谱")

    result = await run_structured_filter_query(
        {"anchor": {"name": "完全不认识的名字"}},
        graph_client=_ExplodingGraphClient(), tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(), term_type_schema={},
    )

    assert result == {"matched_count": 0, "truncated": False, "anchors": []}


async def test_run_structured_filter_query_name_anchor_uses_type_hint_to_disambiguate():
    terms = [
        Term(tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee", aliases=[], term_type="产品"),
        Term(tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee", aliases=[], term_type="类目"),
    ]
    graph_client = _FakeGraphClient(rows=[])

    await run_structured_filter_query(
        {"anchor": {"name": "Coffee", "type_hint": "类目"}},
        graph_client=graph_client, tenant_id="t1", terms=terms,
        confirmed_relation_types=set(),
        term_type_schema={"类目": TermTypeCategory(value="类目", extra_fields=[])},
    )

    assert graph_client.last_resolved.node_key == "类目:Coffee"
```

`_FakeGraphClient.execute_structured_filter_query` 的签名跟 Task 8 敲定的 `Neo4jGraphClient.execute_structured_filter_query(args, *, resolved, tenant_id, term_type_schema)` 保持一致——`args`（`StructuredFilterQueryArgs`）本身没有 `node_key`，锚点定位信息都在单独传入的 `resolved`（`ResolvedAnchor`）里，`_FakeGraphClient` 记录 `resolved` 而不是从 `args` 上取：

```python
class _FakeGraphClient:
    def __init__(self, *, rows=None, group_result=None, error=None, total_count=None) -> None:
        self._rows = rows if rows is not None else []
        self._group_result = group_result
        self._error = error
        self._total_count = total_count if total_count is not None else len(self._rows)
        self.last_args = None
        self.last_resolved = None
        self.last_tenant_id = None

    async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
        self.last_args = args
        self.last_resolved = resolved
        self.last_tenant_id = tenant_id
        if self._error is not None:
            raise self._error
        if self._group_result is not None:
            return self._group_result
        return {"rows": self._rows, "total_count": self._total_count}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v -k run_structured_filter_query`
Expected: FAIL（`run_structured_filter_query` 还不认识新的 `anchor` 结构、不接受 `terms` 参数）。

- [ ] **Step 3: 实现**

在 `app/graphrag/structured_filter_query.py`，`run_structured_filter_query` 顶部加 `from app.graphrag.ontology import Term, resolve_term`（如果还没导入），重写：

```python
async def run_structured_filter_query(
    raw_args: dict,
    *,
    graph_client: "Neo4jGraphClient",
    tenant_id: str,
    terms: list[Term],
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> dict[str, Any]:
    """structured_filter_query_tool 的执行体调用的编排入口：解析→（NameAnchor 时）
    消歧解析→校验→执行→格式化。"""
    try:
        args = parse_structured_filter_query_args(raw_args)
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}

    if isinstance(args.anchor, NameAnchor):
        term = resolve_term(args.anchor.name, terms, term_type_hint=args.anchor.type_hint)
        if term is None:
            return {"matched_count": 0, "truncated": False, "anchors": []}
        resolved = ResolvedAnchor(term_type=term.term_type, node_key=term.node_key)
    else:
        resolved = ResolvedAnchor(term_type=args.anchor.term_type, node_key=None)

    try:
        validate_structured_filter_query(
            args, resolved=resolved,
            confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}

    try:
        result = await graph_client.execute_structured_filter_query(
            args, resolved=resolved, tenant_id=tenant_id, term_type_schema=term_type_schema,
        )
    except Exception as exc:
        return {"error": f"图谱查询执行失败：{exc}"}

    if "groups" in result:
        return result

    rows = result["rows"]
    total_count = result["total_count"]
    payload: dict[str, Any] = {
        "matched_count": total_count,
        "anchors": [
            {
                "standard_name": row["standard_name"],
                "node_key": row["node_key"],
                "term_type": row["term_type"],
                "extra_properties": {
                    k: v
                    for k, v in row["all_properties"].items()
                    if k not in _CORE_TERM_FIELDS and k not in _LEGACY_RESIDUAL_NODE_PROPERTIES
                },
                **({"neighbors": row["neighbors"]} if "neighbors" in row else {}),
            }
            for row in rows
        ],
    }
    if total_count > len(rows):
        payload["truncated"] = True
    return payload
```

（`neighbors` 字段的实际产出是 Task 8 的职责——这里先写好"如果 `execute_structured_filter_query` 返回的行里带了 `neighbors` 键就透传"这个装配逻辑，Task 8 落地 Cypher 层的 `neighbors` 之后不需要再回来改这段代码。`association` 文案不在这里加，留到 Task 9`app/agent/tools.py`层组装最终 LLM 观察结果时再加，跟原 `graph_query_tool` 分支的既有做法一致。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v`
Expected: 之前写的编排测试全部 PASS。**其中依赖 `execute_structured_filter_query` 接受 `resolved` 参数的测试（`test_run_structured_filter_query_resolves_name_anchor_and_uses_node_key` 等）目前会因为 `_FakeGraphClient` 还没实现对应逻辑而失败或者因为 Task 8 还没让真实 `Neo4jGraphClient` 认识这个签名——但 `_FakeGraphClient` 是测试文件自己定义的替身，只要它自己的 `execute_structured_filter_query` 签名接受 `resolved` 参数（上面已经改好），这些测试应该本任务内就能全绿，不依赖 Task 8**。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "feat(graphrag): resolve_term-integrate NameAnchor orchestration in run_structured_filter_query"
```

---

### Task 8: 执行层——锚点二选一定位 + 邻居展开 Cypher

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Consumes: `ResolvedAnchor`/`ExpandSpec`（Task 6）。
- Produces: `execute_structured_filter_query(args, *, resolved, tenant_id, term_type_schema)`（新增 `resolved` 参数，`args.anchor_term_type` 不再被这个方法读取，改读 `resolved`）；`rows` 里每行按 `expand` 是否设置带 `neighbors` 键。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/graphrag/test_neo4j_client.py 末尾

from app.graphrag.structured_filter_query import ExpandSpec, ResolvedAnchor


async def test_execute_structured_filter_query_name_anchor_matches_by_node_key():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="公司"),  # 这一步 anchor 字段本身不再被 execute_structured_filter_query 使用
        constraints=[], expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="公司:Coca-Cola"),
        tenant_id="demo", term_type_schema={},
    )

    assert "node_key: $anchor_node_key" in session.calls[-1][0]
    assert session.calls[-1][1]["anchor_node_key"] == "公司:Coca-Cola"


async def test_execute_structured_filter_query_type_anchor_matches_by_type():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 0}, []])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        tenant_id="demo", term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert "type: $anchor_term_type" in session.calls[-1][0]


async def test_execute_structured_filter_query_expand_any_relation_type_omits_type_segment():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="both"),
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    query = session.calls[-1][0]
    assert "OPTIONAL MATCH" in query
    assert "[r*1..1]" in query
    assert ":" not in query.split("[r")[1].split("*")[0]  # 关系类型段为空


async def test_execute_structured_filter_query_expand_specific_relation_type_includes_type_segment():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type="BELONG_TO", direction="outgoing"),
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    query = session.calls[-1][0]
    assert "[r:BELONG_TO*1..1]->" in query


async def test_execute_structured_filter_query_expand_direction_incoming_uses_left_arrow():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="incoming"),
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    assert "<-[r*1..1]-" in session.calls[-1][0]


async def test_execute_structured_filter_query_expand_limit_applies_before_optional_match():
    """LIMIT 必须约束的是锚点数，不是展开后的行数——WITH...LIMIT 必须出现在
    OPTIONAL MATCH 之前。"""
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, []])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="both"),
        group_by=None, limit=5,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    query = session.calls[-1][0]
    assert query.index("LIMIT $limit") < query.index("OPTIONAL MATCH")


async def test_execute_structured_filter_query_expand_returns_empty_list_when_no_neighbors():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="both"),
        group_by=None, limit=20,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    assert result["rows"][0]["neighbors"] == []


async def test_execute_structured_filter_query_no_expand_rows_have_no_neighbors_key():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=None, group_by=None, limit=20,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    assert "neighbors" not in result["rows"][0]
```

（这些测试用 `FakeSession`/`FakeResult` 构造的返回数据是"事先写死的观察值"，不是真的验证 Cypher 会实际产出这个数据形状——`neighbors` 字段是否真的被正确填充，靠断言生成的 Cypher 文本本身（`OPTIONAL MATCH`/`collect(...)` 等关键字），跟这个文件里现有测试的验证方式（断言 `session.last_query` 文本）保持同一套风格，不引入需要真实 Neo4j 的集成测试。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: 大量 FAIL（`execute_structured_filter_query` 还不接受 `resolved` 参数，也不支持 `expand`）。这一步之后 Task 4 写的、还没适配 `resolved` 参数的测试也会失败——本任务 Step 5 一并修。

- [ ] **Step 3: 实现**

在 `app/graphrag/neo4j_client.py`：

1. 顶部导入区补上 `NameAnchor`/`TypeAnchor`/`ExpandSpec`/`ResolvedAnchor`（如果只导入了部分类型，把这几个都加进 `from app.graphrag.structured_filter_query import (...)`）。

2. 新增邻居展开 Cypher 片段构造函数：

```python
def _build_expand_clause(expand: ExpandSpec) -> str:
    rel_pattern = f":{expand.relation_type}" if expand.relation_type else ""
    if expand.direction == "outgoing":
        arrow_in, arrow_out = "", "->"
    elif expand.direction == "incoming":
        arrow_in, arrow_out = "<-", ""
    else:
        arrow_in, arrow_out = "", ""
    return (
        f"OPTIONAL MATCH p = (anchor){arrow_in}[r{rel_pattern}*1..{expand.hops}]{arrow_out}"
        "(neighbor:Term {tenant_id: $tenant_id}) "
        "WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id) AND neighbor <> anchor"
    )


_EXPAND_RETURN_FRAGMENT = (
    "collect(DISTINCT CASE WHEN neighbor IS NULL THEN NULL "
    "ELSE {related_name: neighbor.standard_name, "
    "relation_type: [rel IN r | type(rel)][-1], hops: length(p)} END) AS neighbors"
)
```

3. `execute_structured_filter_query` 重写（签名 + 锚点定位 + WHERE/计数/取行 + expand 拼接）：

```python
    async def execute_structured_filter_query(
        self,
        args: StructuredFilterQueryArgs,
        *,
        resolved: ResolvedAnchor,
        tenant_id: str,
        term_type_schema: dict[str, TermTypeCategory],
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """...原有 docstring 保留，追加一句：resolved 由调用方（run_structured_
        filter_query）解析 args.anchor 之后传入，本方法按 resolved.node_key 是否
        为空二选一决定锚点怎么定位，不再自己判断 args.anchor 是哪种模式。"""
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if resolved.node_key is not None:
            anchor_match = "MATCH (anchor:Term {tenant_id: $tenant_id, node_key: $anchor_node_key})"
            params["anchor_node_key"] = resolved.node_key
        else:
            anchor_match = "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type})"
            params["anchor_term_type"] = resolved.term_type

        where_clauses: list[str] = []
        for i, constraint in enumerate(args.constraints):
            if isinstance(constraint, AttributeConstraint):
                value_param = f"value_{i}"
                params[value_param] = constraint.value
                where_clauses.append(
                    _comparison_expression(
                        prop_expr=f"anchor.{constraint.field}", operator=constraint.operator,
                        param_name=value_param,
                        cast=_resolve_cast(
                            term_type=resolved.term_type, field=constraint.field,
                            term_type_schema=term_type_schema,
                        ),
                    )
                )
                continue
            if args.group_by is not None and args.group_by.constraint_index == i:
                continue
            match_pattern, hop_params = _build_hop_match_pattern(constraint.hops, prefix=f"c{i}")
            params.update(hop_params)
            target_value_param = f"c{i}_target_value"
            params[target_value_param] = constraint.target_value
            last_var = f"c{i}_hop{len(constraint.hops) - 1}"
            comparison = _comparison_expression(
                prop_expr=f"{last_var}.{constraint.target_field}",
                operator=constraint.target_operator, param_name=target_value_param,
                cast=_resolve_cast(
                    term_type=constraint.hops[-1].target_term_type, field=constraint.target_field,
                    term_type_schema=term_type_schema,
                ),
            )
            where_clauses.append(f"EXISTS {{ {match_pattern} WHERE {comparison} }}")

        where_sql = " AND ".join(where_clauses) if where_clauses else "true"

        if args.group_by is not None:
            group_constraint = args.constraints[args.group_by.constraint_index]
            assert isinstance(group_constraint, RelationConstraint)
            match_pattern, hop_params = _build_hop_match_pattern(
                group_constraint.hops, prefix=f"g{args.group_by.constraint_index}"
            )
            params.update(hop_params)
            last_var = f"g{args.group_by.constraint_index}_hop{len(group_constraint.hops) - 1}"
            query = (
                f"{anchor_match} "
                f"{match_pattern} "
                f"WHERE {where_sql} "
                f"RETURN {last_var}.{group_constraint.target_field} AS value, count(DISTINCT anchor) AS count "
                "ORDER BY count DESC"
            )
            async with self._driver.session() as session:
                result = await session.run(query, params)
                rows = await result.data()
            return {"groups": rows}

        count_query = f"{anchor_match} WHERE {where_sql} RETURN count(anchor) AS total"
        return_fields = (
            "anchor.standard_name AS standard_name, anchor.node_key AS node_key, "
            "anchor.type AS term_type, properties(anchor) AS all_properties"
        )
        if args.expand is not None:
            expand_clause = _build_expand_clause(args.expand)
            rows_query = (
                f"{anchor_match} WHERE {where_sql} "
                "WITH anchor ORDER BY anchor.node_key LIMIT $limit "
                f"{expand_clause} "
                f"RETURN {return_fields}, {_EXPAND_RETURN_FRAGMENT}"
            )
        else:
            rows_query = (
                f"{anchor_match} WHERE {where_sql} "
                f"RETURN {return_fields} "
                "ORDER BY anchor.node_key LIMIT $limit"
            )
        rows_params = {**params, "limit": args.limit}
        async with self._driver.session() as session:
            count_result = await session.run(count_query, params)
            total_count = (await count_result.data())[0]["total"]
            rows_result = await session.run(rows_query, rows_params)
            rows = await rows_result.data()
        return {"rows": rows, "total_count": total_count}
```

（`NameAnchor` 模式下 `args.constraints` 可能为空，`where_sql` 退化成 `"true"`——`WHERE true` 在 Cypher 里合法，等价于不过滤，行为正确。）

- [ ] **Step 4: 修复 Task 4 写的、还没适配 `resolved` 参数的测试**

Task 4 里写的几个测试（`test_execute_structured_filter_query_casts_numeric_standard_name_comparison` 等）用的是 `StructuredFilterQueryArgs(anchor_term_type=...)` 旧构造方式，Task 6 已经把它们的 `parse_structured_filter_query_args` 调用点改掉了，但这几个是**直接构造** `StructuredFilterQueryArgs`，需要单独改：把 `anchor_term_type="销量"` 改成 `anchor=TypeAnchor(term_type="销量")`（`StructuredFilterQueryArgs` 新增了 `expand` 必填字段，一并补 `expand=None`），调用 `execute_structured_filter_query` 时加 `resolved=ResolvedAnchor(term_type="销量", node_key=None)` 参数。逐一过一遍 Task 4 加的那几个测试，照此模式改。

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(graphrag): support node_key-anchored queries and neighbor expansion in execute_structured_filter_query"
```

---

### Task 9: `app/agent/tools.py`——移除 `graph_query_tool`，统一 schema

**Files:**
- Modify: `app/agent/tools.py`
- Test: `tests/agent/test_tools.py`

**Interfaces:**
- Produces: 删除 `GRAPH_QUERY_TOOL_SCHEMA`/`graph_query_tool()`/`GraphQueryToolResult`；`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 换成 `anchor`/`expand` 结构；`structured_filter_query_tool()` 签名新增 `terms: list[Term]`。

- [ ] **Step 1: 改写既有测试**

删除 `tests/agent/test_tools.py` 里这些针对 `graph_query_tool` 的测试函数（整个函数体删掉，不留残留 import）：`test_graph_query_tool_resolves_alias_and_returns_subgraph`、`test_graph_query_tool_returns_unresolved_without_querying_graph`、`test_graph_query_tool_uses_entity_type_to_disambiguate`、`test_graph_query_tool_returns_unresolved_when_ambiguous_without_entity_type`、`test_graph_query_tool_resolves_alias_even_when_standard_name_collides_with_another_type`、`FakeGraphClient` 类定义（如果没有其它测试用到，一并删）、`_TERMS` 常量（如果没有其它测试用到）。

`test_tool_schemas_do_not_expose_tenant_id` 改成：

```python
def test_tool_schemas_do_not_expose_tenant_id():
    for schema in (VECTOR_SEARCH_TOOL_SCHEMA, STRUCTURED_FILTER_QUERY_TOOL_SCHEMA):
        properties = schema["function"]["parameters"]["properties"]
        assert "tenant_id" not in properties
```

顶部 import 改成：

```python
from app.agent.tools import (
    STRUCTURED_FILTER_QUERY_TOOL_SCHEMA,
    VECTOR_SEARCH_TOOL_SCHEMA,
    vector_search_tool,
)
```

`test_structured_filter_query_tool_delegates_to_run_structured_filter_query` 改成新 schema：

```python
async def test_structured_filter_query_tool_delegates_to_run_structured_filter_query():
    from app.agent.tools import structured_filter_query_tool
    from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            return {"rows": [], "total_count": 0}

    result = await structured_filter_query_tool(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        tenant_id="muji", terms=[], graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )},
    )

    assert result == {"matched_count": 0, "truncated": False, "anchors": []}
```

新增测试覆盖 `anchor.name` 用法（原 `graph_query_tool` 覆盖的场景，迁移到新接口）：

```python
async def test_structured_filter_query_tool_resolves_name_anchor():
    from app.agent.tools import structured_filter_query_tool
    from app.graphrag.ontology import Term
    from app.graphrag.ontology_categories import TermTypeCategory

    terms = [Term(
        tenant_id="t1", node_key="示例错误码E502", standard_name="示例错误码E502",
        aliases=["网关超时示例"], term_type="error_code",
    )]

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            assert resolved.node_key == "示例错误码E502"
            return {"rows": [{
                "standard_name": "示例错误码E502", "node_key": "示例错误码E502",
                "term_type": "error_code", "all_properties": {},
            }], "total_count": 1}

    result = await structured_filter_query_tool(
        {"anchor": {"name": "网关超时示例"}},
        tenant_id="t1", terms=terms, graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    assert result["matched_count"] == 1
    assert result["anchors"][0]["standard_name"] == "示例错误码E502"


def test_structured_filter_query_tool_schema_supports_anchor_name_and_expand():
    from app.agent.tools import STRUCTURED_FILTER_QUERY_TOOL_SCHEMA
    properties = STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert "anchor" in properties
    assert "expand" in properties
    assert "graph_query_tool" not in str(STRUCTURED_FILTER_QUERY_TOOL_SCHEMA)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py -v`
Expected: FAIL（`GRAPH_QUERY_TOOL_SCHEMA`/`graph_query_tool` 还存在，`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 还是旧结构，`structured_filter_query_tool()` 还不接受 `terms`）。

- [ ] **Step 3: 实现**

在 `app/agent/tools.py`：

1. 删除 `GRAPH_QUERY_TOOL_SCHEMA`、`GraphQueryToolResult`、`graph_query_tool()` 整段定义；删除现在没用到的 `from app.graphrag.ontology import Term, resolve_term`（`resolve_term` 不再在这个文件用到——它移到 `structured_filter_query.py` 里用了，`Term` 类型注解还需要保留，因为 `structured_filter_query_tool` 新签名要用）。

2. `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 整体替换成：

```python
STRUCTURED_FILTER_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": (
            "在知识图谱里查询实体——支持三种用法，可以组合使用：\n"
            "1. 已知实体名，查它是什么/关联着什么：anchor.name（会做别名模糊匹配）+ expand。\n"
            "2. 不知道具体实体名，按条件筛选一批满足条件的实体，"
            "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」"
            "「xx有多少个/数量是多少」这类问题：anchor.term_type + constraints。\n"
            "3. 上述两种可以叠加 expand，展开命中锚点的邻居关系。\n"
            "「xx类目/公司下有多少个yy」这类需要先确定xx是什么、再数yy数量的问题，"
            "通常需要 anchor.name 消歧 + constraints 筛选组合两次调用，"
            "或者一次调用里 anchor.term_type 直接按关系条件筛选（见 constraints.kind=relation）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "anchor": {
                    "type": "object",
                    "description": "起点定位方式，二选一",
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "已知的实体名称或别名"},
                                "type_hint": {
                                    "type": "string",
                                    "description": "该实体的类型（可选，同名实体存在多个类型时用于消歧）",
                                },
                            },
                            "required": ["name"],
                        },
                        {
                            "type": "object",
                            "properties": {
                                "term_type": {
                                    "type": "string",
                                    "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                                },
                            },
                            "required": ["term_type"],
                        },
                    ],
                },
                "constraints": {
                    "type": "array",
                    "description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，直接用解析出的锚点）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["attribute", "relation"],
                                "description": "attribute：直接比较锚点实体自己的字段；relation：经过关系跳到目标实体再比较",
                            },
                            "field": {
                                "type": "string",
                                "description": "kind=attribute 时必填：要比较的字段名（standard_name 或该实体类型已声明的属性字段名）",
                            },
                            "operator": {
                                "type": "string",
                                "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                         "all_lte", "all_gte", "any_lte", "any_gte"],
                                "description": "比较运算符，实际可用范围取决于字段类型",
                            },
                            "value": {"description": "kind=attribute 时必填：比较的目标值"},
                            "hops": {
                                "type": "array",
                                "description": "kind=relation 时必填：从锚点出发的关系跳数组，最多2跳",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "relation_type": {"type": "string", "description": "关系类型，如 HAS_VARIANT"},
                                        "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
                                        "target_term_type": {"type": "string", "description": "这一跳到达的实体类型"},
                                    },
                                    "required": ["relation_type", "direction", "target_term_type"],
                                },
                            },
                            "target_field": {
                                "type": "string",
                                "description": "kind=relation 时必填：在最后一跳到达的实体上比较哪个字段",
                            },
                            "target_operator": {
                                "type": "string",
                                "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                         "all_lte", "all_gte", "any_lte", "any_gte"],
                                "description": "kind=relation 时必填：对 target_field 用的运算符",
                            },
                            "target_value": {"description": "kind=relation 时必填：比较的目标值"},
                        },
                        "required": ["kind"],
                    },
                },
                "expand": {
                    "type": ["object", "null"],
                    "description": "可选：展开命中锚点的邻居关系",
                    "properties": {
                        "hops": {"type": "integer", "enum": [1, 2], "description": "展开几跳，默认1"},
                        "relation_type": {
                            "type": ["string", "null"],
                            "description": "只展开这种关系类型；不传或传 null 表示任意类型",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["outgoing", "incoming", "both"],
                            "description": "关系方向，默认 both",
                        },
                    },
                },
                "group_by": {
                    "type": ["object", "null"],
                    "description": "可选：按某个字段做 distinct 值统计而不是返回实体列表本身",
                    "properties": {
                        "constraint_index": {
                            "type": "integer",
                            "description": "指向 constraints 数组里某个 kind=relation 约束的下标，按它的 target_field 分组",
                        },
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果的最大条数，默认20——预期命中数量较多时"
                                   "（如宽泛的数值区间过滤），请设置一个合理的值避免返回过多结果",
                },
            },
            "required": ["anchor"],
        },
    },
}
```

3. `structured_filter_query_tool()` 签名新增 `terms`：

```python
async def structured_filter_query_tool(
    arguments: dict[str, Any],
    *,
    tenant_id: str,
    terms: list[Term],
    graph_client: GraphClientProtocol,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> dict[str, Any]:
    """structured_filter_query_tool 的实际执行体，薄封装
    structured_filter_query.py::run_structured_filter_query。"""
    return await run_structured_filter_query(
        arguments, terms=terms, graph_client=graph_client, tenant_id=tenant_id,
        confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/agent/tools.py tests/agent/test_tools.py
git commit -m "refactor(agent): remove graph_query_tool, unify structured_filter_query_tool schema"
```

---

### Task 10: `app/agent/planner.py`——`_dispatch_tool_call` 重写 + `_TOOL_SCHEMAS`

**Files:**
- Modify: `app/agent/planner.py`
- Test: `tests/agent/test_planner.py`

**Interfaces:**
- Consumes: Task 9 的 `structured_filter_query_tool()` 新签名。
- Produces: `_dispatch_tool_call` 不再识别 `graph_query_tool`；`structured_filter_query_tool` 分支透传 `terms`，对每个匹配锚点的 `neighbors`（如果有）逐条补 `association` 字段。

- [ ] **Step 1: 改写既有测试**

删除 `tests/agent/test_planner.py` 里这些测试函数：`test_run_tool_calls_executes_graph_query_tool`、`test_run_tool_calls_passes_entity_type_argument_to_graph_query_tool`、`FakeGraphClient` 类（第223-231行，如果没有其它测试用到）、`FakeGraphClientWithTwoHopRow` 类和 `test_run_tool_calls_annotates_two_hop_subgraph_rows_with_association`。

`_TERMS` 常量（第212-220行）保留（`_TERMS` 本身跟工具名无关，是通用的术语表测试数据，下面新测试还会用到）。

第434、473行两处 `{"id": "call_2", "name": "graph_query_tool", "arguments": ...}`——看一下这两处所在的完整测试函数（并发调用/异常处理相关），把 `"name": "graph_query_tool"` 改成 `"name": "structured_filter_query_tool"`，`"arguments"` 里的内容如果跟 `entity_name`/`entity_type` 相关，改成对应的 `{"anchor": {...}}` 形状；如果这两个测试只是用它们做"跟 vector_search_tool 并发"或"其中一个失败"的占位符、不深究具体参数内容，直接把工具名换掉、参数改成合法的 `structured_filter_query_tool` 形状即可（比如 `'{"anchor": {"term_type": "SKU"}, "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": "x"}]}'`），不用额外验证这两个测试原本没验证的东西。

新增测试覆盖 `structured_filter_query_tool` 的 `anchor.name` + `expand` 编排、`association` 标注（迁移原 `graph_query_tool` 场景的覆盖）：

```python
class FakeGraphClientForStructuredQuery:
    def __init__(self, *, rows=None, total_count=None) -> None:
        self._rows = rows if rows is not None else []
        self._total_count = total_count if total_count is not None else len(self._rows)
        self.queried_tenant_ids: list[str] = []

    async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
        self.queried_tenant_ids.append(tenant_id)
        return {"rows": self._rows, "total_count": self._total_count}


async def test_run_tool_calls_executes_structured_filter_query_tool_with_name_anchor():
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"anchor": {"name": "网关超时示例"}}',
            }
        ],
    }

    graph_client = FakeGraphClientForStructuredQuery(rows=[{
        "standard_name": "示例错误码E502", "node_key": "示例错误码E502", "term_type": "error_code",
        "all_properties": {},
    }])
    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=_TERMS,
        graph_client=graph_client,
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    tool_message = update["planner_messages"][-1]
    assert "示例错误码E502" in tool_message["content"]
    assert graph_client.queried_tenant_ids == ["t1"]


async def test_run_tool_calls_annotates_expand_neighbors_with_association():
    """expand 返回的 neighbors 要按 hops 标注 association 文案——原
    graph_query_tool 分支的既有行为，迁移到 structured_filter_query_tool。"""
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"anchor": {"name": "网关超时示例"}, "expand": {"hops": 2}}',
            }
        ],
    }

    graph_client = FakeGraphClientForStructuredQuery(rows=[{
        "standard_name": "示例错误码E502", "node_key": "示例错误码E502", "term_type": "error_code",
        "all_properties": {},
        "neighbors": [{"related_name": "示例登录模块", "relation_type": "RELATED_TO", "hops": 2}],
    }])
    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=_TERMS,
        graph_client=graph_client,
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    tool_message = update["planner_messages"][-1]
    parsed = json.loads(tool_message["content"])
    assert parsed["anchors"][0]["neighbors"][0]["association"] == "间接关联（经过 2 跳）"


def test_tool_schemas_no_longer_include_graph_query_tool():
    from app.agent.planner import _TOOL_SCHEMAS
    names = [s["function"]["name"] for s in _TOOL_SCHEMAS]
    assert "graph_query_tool" not in names
    assert "structured_filter_query_tool" in names
```

（顶部 import 需要加 `from app.graphrag.ontology_categories import TermTypeCategory`，如果文件还没有这个导入。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -v`
Expected: FAIL（`_dispatch_tool_call` 还认识 `graph_query_tool`、`structured_filter_query_tool` 分支不传 `terms`、不做 `association` 标注）。

- [ ] **Step 3: 实现**

在 `app/agent/planner.py`：

1. `_TOOL_SCHEMAS` 去掉 `GRAPH_QUERY_TOOL_SCHEMA`：

```python
_TOOL_SCHEMAS = [VECTOR_SEARCH_TOOL_SCHEMA, STRUCTURED_FILTER_QUERY_TOOL_SCHEMA]
```

`from app.agent.tools import (...)` 导入区去掉 `GRAPH_QUERY_TOOL_SCHEMA`/`graph_query_tool`。

2. `_dispatch_tool_call` 删除整个 `if name == "graph_query_tool":` 分支，`structured_filter_query_tool` 分支改成：

```python
    if name == "structured_filter_query_tool":
        if graph_client is None or confirmed_relation_types is None or term_type_schema is None or not terms:
            return json.dumps({"error": "structured_filter_query_tool 未配置"}, ensure_ascii=False), []
        observation = await structured_filter_query_tool(
            arguments, tenant_id=tenant_id, terms=terms, graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
        for anchor in observation.get("anchors", []):
            for neighbor in anchor.get("neighbors", []):
                neighbor["association"] = describe_association(neighbor.get("hops", 1))
        return json.dumps(observation, ensure_ascii=False), []
```

（`describe_association` 已经在文件顶部导入——原 `graph_query_tool` 分支用过，`from app.graphrag.term_guard import GraphClientProtocol, describe_association` 这行不用改。`not terms` 这个新增守卫条件：`terms` 是 `list[Term] | None`，空列表和 `None` 都应该判定为"未配置"——`anchor.name` 模式下没有术语表就没法做任何解析，`anchor.term_type` 模式虽然理论上不需要 `terms`，但为了守卫条件简单统一，两种模式都要求 `terms` 非空——这跟原来 `graph_query_tool` 分支 `if not (terms and graph_client is not None):` 的守卫风格一致，不是新引入的限制。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/agent/planner.py tests/agent/test_planner.py
git commit -m "refactor(agent): route structured_filter_query_tool through unified dispatch, drop graph_query_tool branch"
```

---

### Task 11: `app/agent/graph.py`——系统提示词更新

**Files:**
- Modify: `app/agent/graph.py`
- Test: `tests/agent/test_graph_planner.py`

**Interfaces:**
- Consumes: 无（纯文案）。

- [ ] **Step 1: 检查既有测试是否有依赖旧提示词文本的断言**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_planner.py -v -k prompt`
先确认有没有测试直接断言 `_PLANNER_SYSTEM_PROMPT` 的具体文本内容（如果有，这一步先看一眼断言的是什么，判断改了文案后要不要同步改断言——大概率这类测试只断言"消息列表第一条是 system role"或"包含某个工具名字符串"，不会断言完整文案，正常情况这步不需要改测试）。

- [ ] **Step 2: 实现**

```python
_PLANNER_SYSTEM_PROMPT = (
    "你是客服问答助手。可以调用 vector_search_tool 检索知识库、"
    "structured_filter_query_tool 查询知识图谱——支持已知实体名查询关联信息"
    "（anchor.name，会做别名模糊匹配）、按数值区间/精确匹配/关系条件反查一批满足条件的实体"
    "（anchor.term_type + constraints，适用于「有没有xx以上的」「比xx大的有哪些」"
    "「xx有多少个/数量是多少」这类问题）、以及展开某个实体的关联关系（expand）。"
    "看到「多少个」「数量」等计数意图时，必须以这个工具的 matched_count 为准给出确定数字，"
    "不能仅凭检索到的文档片段或邻居关系列表猜测，也不能因为一次调用没查到就直接放弃——"
    "先消歧、再筛选计数，通常需要两次调用。"
    "有足够信息时直接给出最终答案，不要编造资料中没有的内容；"
    "信息不足以回答时也不要编造。"
)
```

- [ ] **Step 3: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_planner.py tests/agent/test_graph.py -v`
Expected: 全部 PASS（这两个文件里如果有依赖 `graph_query_tool` 的端到端测试，会在这一步或 Task 12 暴露出来——如果这一步就有 FAIL，说明有遗漏的测试要迁移，参照 Task 12 的模式处理，不用等到 Task 12 才发现）。

- [ ] **Step 4: 提交**

```bash
git add app/agent/graph.py
git commit -m "docs(agent): update planner system prompt for unified structured_filter_query_tool"
```

---

### Task 12: 全量回归 + 文档同步

**Files:**
- Modify: `docs/AGENT_PLANNER_DESIGN.md`
- Test: 全量后端测试

**Interfaces:**
- 无新接口，收尾任务。

- [ ] **Step 1: 全量搜索确认没有遗漏的 `graph_query_tool` 引用**

Run: `grep -rn "graph_query_tool\|GraphQueryToolResult\|GRAPH_QUERY_TOOL_SCHEMA" app/ tests/ docs/AGENT_PLANNER_DESIGN.md`
Expected: 只剩 `docs/AGENT_PLANNER_DESIGN.md` 里的引用（下一步处理）；如果 `app/`/`tests/` 下还有残留，回到对应任务补漏。

- [ ] **Step 2: 更新 `docs/AGENT_PLANNER_DESIGN.md`**

第111-112行的工具能力表格（原来分别描述 `graph_query_tool`/`structured_filter_query_tool` 两行）合并成一行：

```markdown
| `structured_filter_query_tool` | `anchor`（`{name, type_hint?}` 或 `{term_type}`）, `constraints: list`, `expand`（可选，`{hops, relation_type?, direction}`）, `group_by`（可选）, `limit: int`（可选，默认20） | `tenant_id`、该租户已确认的 term_type/relation_type schema、`terms`（`anchor.name` 消歧用） | 已知实体查关联信息 / 按属性关系条件反查一批实体 / 展开锚点邻居，三种能力统一入口；内部调用 `run_structured_filter_query()`（解析→resolve_term 消歧（`anchor.name` 时）→按已确认 schema 校验→`graph_client.execute_structured_filter_query()`） |
```

正文里提到 `graph_query_tool`/`GraphQueryToolResult` 的地方（第27、203、232、249行附近，实际行号以当前文件为准，先用 `grep -n "graph_query_tool"` 定位）——删掉对 `graph_query_tool` 的单独提及，改成"合并进 structured_filter_query_tool"的说法，或者直接删除已经不成立的历史描述（比如第203行"连续调用2种不同工具（先 graph_query_tool 再 vector_search_tool）"这种举例场景，改成用现有工具名的等价场景）。

- [ ] **Step 3: 全量后端回归**

Run: `.venv/Scripts/python.exe -m pytest -q > /tmp_full_run.txt 2>&1; cat /tmp_full_run.txt`（Windows 环境下把 `/tmp_full_run.txt` 换成 scratchpad 目录下的实际路径，重定向到文件再读，不要信任后台/管道跑测试时可能出现的虚假超时状态——见本仓库这类环境的既知问题）

Expected: 除了已知的、跟本次改动无关的环境性/预先存在失败（如果发现新的失败，必须先查清楚是不是本次改动引入的回归，不能假定"跟以前一样"就跳过）之外，全绿。

- [ ] **Step 4: 前端类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 干净。

- [ ] **Step 5: 人工验证（可选但强烈建议）**

按 Task 5 的方式启动前后端，用真实/demo 环境重新问一遍"coke-cola类目下有多少个订单"和"销量大于50的有多少个订单"（后者需要先在管理后台把 demo 租户的"销量"term type 编辑成 `standard_name_value_type=number`），确认现在能给出基于图谱的真实数字回答，不再转人工。

- [ ] **Step 6: 提交**

```bash
git add docs/AGENT_PLANNER_DESIGN.md
git commit -m "docs(agent): document unified structured_filter_query_tool in planner design doc"
```
