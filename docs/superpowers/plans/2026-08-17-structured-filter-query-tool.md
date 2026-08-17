# 结构化过滤查询工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Agent 新增一个独立工具 `structured_filter_query_tool`，能按数值区间/精确匹配/关系约束筛选知识图谱里满足条件的实体，覆盖 `graph_query_tool`（已知实体→查邻域）结构性做不到的"不知道具体实体、按条件反查"这类问法。

**Architecture:** 新增 `app/graphrag/structured_filter_query.py` 承担参数解析+schema 校验+返回形状编排；`Neo4jGraphClient` 新增一个方法负责 Cypher 构造+执行（属性字段名走 `t[$field]` 参数化动态访问，不做字符串插值；`relation_type` 沿用 `merge_relation` 已确立的"格式校验+已确认 schema 成员校验"防线）。Agent 侧按 `graph_query_tool` 的现有接入模式（`app/agent/tools.py` 定义 schema+执行体，`app/agent/planner.py` 注册+分发）接入，`confirmed_relation_types`/`term_type_schema` 两份只读 schema 数据跟 `terms` 一样按请求预加载一次、往下传（不给 Agent 工具层引入一个存活的 SQLite 连接）。顺带修复 `ontology_categories.py` 里 `extra_fields` 字段名缺失格式校验的问题，并把"给已确认的数值/字符串字段建 Neo4j 索引"接进 term-type 声明流程。

**Tech Stack:** Python 3.12, aiosqlite, neo4j async driver（经 `Neo4jGraphClient` 封装）, FastAPI Depends, pytest + pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-08-17-structured-filter-query-tool-design.md`

## Global Constraints

- 字段名（`field`/`target_field`）一律走 Cypher 动态属性访问 `t[$param]`，绝不字符串插值进查询文本——spec 第5.1节。
- `relation_type` 无法参数化绑定，必须先过格式校验（`^[A-Z][A-Z0-9_]{0,63}$`）再过"该租户已确认关系类型"成员校验，两层都过才能拼进 Cypher——spec 第4节。
- `constraints` 至少一项，不允许空约束全量扫描——spec 第3.2节。
- `hops` 每个 relation 型约束最多2跳——spec 第3.2节。
- `hops` 每一跳显式指定 `direction`（`outgoing`/`incoming`），不给默认值——spec 第3.2节。
- 校验失败返回结构化 `{"error": ...}` 观察结果，不是让异常向上传播到 LLM 看不到具体原因的地方——spec 第4节。
- 不设服务端 `limit` 硬上限，完全由调用方（LLM）传参控制——spec 第3.2节（有意的风险接受，不是遗漏）。
- `Neo4jGraphClient` 新增方法接收的是已经通过校验的结构化参数（dataclass），不重复做 schema 校验、也不接触 SQLite——校验和 Cypher 执行是两层职责，spec 第5节。
- Agent 工具层的新 schema 数据（`confirmed_relation_types`/`term_type_schema`）按请求预加载一次、以只读数据形式往下传，不在 `app/agent/planner.py`/`app/agent/tools.py` 里新开一个存活的 `aiosqlite.Connection`——跟随 `terms: list[Term]` 已经确立的既有模式（`app/api/deps.py::get_terms`）。

---

### Task 1: `ontology_categories.py` 字段名格式校验

**Files:**
- Modify: `app/graphrag/ontology_categories.py`
- Test: `tests/graphrag/test_ontology_categories.py`

**Interfaces:**
- Consumes: 无新依赖。
- Produces: `_validate_extra_field_specs` 现在额外校验 `ExtraFieldSpec.name` 的字符集，供 Task 2 的索引建立环节安全地把字段名拼进 `CREATE INDEX` 语句文本（字段名来源是已经过这层校验的声明，不是 LLM 运行时可控参数）。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_ontology_categories.py` 找到已有的 `create_term_type`/`InvalidExtraFieldTypeError` 相关测试区域，追加：

```python
async def test_create_term_type_rejects_extra_field_with_invalid_name_characters():
    conn = await _connect()
    with pytest.raises(InvalidExtraFieldTypeError):
        await create_term_type(
            conn, tenant_id="t1", value="Product",
            extra_fields=[ExtraFieldSpec(name="numeric value", value_type="number")],
        )


async def test_create_term_type_accepts_extra_field_with_underscore_name():
    conn = await _connect()
    await create_term_type(
        conn, tenant_id="t1", value="Product",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    result = await list_term_types(conn, "t1")
    assert result[0].extra_fields[0].name == "numeric_value"
```

（`_connect()`/导入按该测试文件已有的辅助函数/import 写法接，不要重新定义。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_ontology_categories.py -k "invalid_name_characters or underscore_name" -v`
Expected: `test_create_term_type_rejects_extra_field_with_invalid_name_characters` FAIL（当前实现不校验字符集，不会抛异常）。

- [ ] **Step 3: 实现**

在 `app/graphrag/ontology_categories.py` 顶部（`_VALID_EXTRA_FIELD_VALUE_TYPES` 定义附近）新增：

```python
import re

_EXTRA_FIELD_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")
```

把 `_validate_extra_field_specs` 改成：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_ontology_categories.py -v`
Expected: 全部 PASS（含新增的2个 + 已有的全部回归）。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology_categories.py tests/graphrag/test_ontology_categories.py
git commit -m "fix(graphrag): reject extra_field names with unsafe characters"
```

---

### Task 2: Neo4j 索引跟随字段声明建立

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `app/api/admin_ontology_routes.py`
- Test: `tests/graphrag/test_neo4j_client.py`
- Test: `tests/api/test_admin_ontology_routes.py`

**Interfaces:**
- Consumes: Task 1 的字段名格式校验（保证传进来的字段名不会破坏 `CREATE INDEX` 语句文本）；`app.graphrag.ontology_categories.ExtraFieldSpec(name, value_type)`。
- Produces: `Neo4jGraphClient.ensure_extra_field_indexes(self, *, tenant_id: str, term_type: str, extra_fields: list[ExtraFieldSpec]) -> None`，供本任务的路由改造调用，也供未来 ETL/其它声明入口复用。

- [ ] **Step 1: 写失败的测试（neo4j_client 层）**

在 `tests/graphrag/test_neo4j_client.py` 追加（复用文件已有的 `FakeSession`/`FakeDriver`）：

```python
async def test_ensure_extra_field_indexes_creates_index_per_scalar_field():
    from app.graphrag.ontology_categories import ExtraFieldSpec

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_extra_field_indexes(
        tenant_id="muji", term_type="Product",
        extra_fields=[
            ExtraFieldSpec(name="numeric_value", value_type="number"),
            ExtraFieldSpec(name="dims", value_type="number[]"),
            ExtraFieldSpec(name="md_no", value_type="string"),
        ],
    )

    queries = [call[0] for call in session.calls]
    assert any("t.numeric_value" in q for q in queries)
    assert any("t.md_no" in q for q in queries)
    assert not any("t.dims" in q for q in queries)  # number[] 不建标量索引，见 spec 第6节
    assert len(queries) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_neo4j_client.py -k ensure_extra_field_indexes -v`
Expected: FAIL（`ensure_extra_field_indexes` 尚不存在）。

- [ ] **Step 3: 实现（neo4j_client.py）**

在 `Neo4jGraphClient` 类里、`ensure_tenant_scoped_schema` 方法附近新增：

```python
    async def ensure_extra_field_indexes(
        self, *, tenant_id: str, term_type: str, extra_fields: list["ExtraFieldSpec"]
    ) -> None:
        """给某个 term_type 已确认的 string/number/integer 属性字段建 Neo4j property
        index，供 structured_filter_query_tool 的属性过滤在大数据量下不做全表扫描
        （见 docs/superpowers/specs/2026-08-17-structured-filter-query-tool-design.md
        第6节）。number[] 字段不建——Neo4j 对列表属性的 range 索引支持有限，逐元素
        谓词（all_lte/any_gte 等）也用不上标量索引。

        字段名走字符串插值拼进 CREATE INDEX 语句（Cypher 的索引/属性名语法本身无法
        参数化），但这里的字段名来源是已经过 ontology_categories.py 格式校验
        （^[a-zA-Z_][a-zA-Z0-9_]{0,63}$）的声明，不是 LLM 运行时可控参数，风险性质
        与结构化查询工具里 field/target_field 完全不同，不需要走那套校验链。

        tenant_id 拼进索引名（term_tenant_{tenant_id}_{term_type}_{field} 里没有直接
        用 tenant_id，索引条件本身按 (tenant_id, type, field) 三元组建，索引名只需要
        全局唯一、可重复执行——IF NOT EXISTS 保证幂等，同一个字段被多个租户声明时
        （不同 term_type 名字下）不会冲突，因为 IF NOT EXISTS 只按索引名去重，这里
        索引名同时含 term_type 和字段名，不同租户共用同一个 term_type 名字时会共享
        同一条索引定义——这是有意的：索引本身是 (tenant_id, type, field) 三列复合
        索引，租户隔离由查询时的 WHERE tenant_id = $tenant_id 保证，索引定义可以
        跨租户共享同一条 DDL，不需要按租户各建一条。
        """
        _SCALAR_VALUE_TYPES = {"string", "number", "integer"}
        async with self._driver.session() as session:
            for spec in extra_fields:
                if spec.value_type not in _SCALAR_VALUE_TYPES:
                    continue
                index_name = f"term_extra_field_{term_type}_{spec.name}_idx"
                await session.run(
                    f"CREATE INDEX {index_name} IF NOT EXISTS "
                    f"FOR (t:Term) ON (t.tenant_id, t.type, t.{spec.name})"
                )
```

在文件顶部的 `TYPE_CHECKING`/import 区加一行（避免和 `app.graphrag.ontology_categories` 产生运行时循环导入——`ontology_categories.py` 不导入 `neo4j_client.py`，这里只是类型注解用，按此文件已有的 `from __future__ import annotations` 约定用字符串前向引用即可，不需要额外导入；上面代码里 `"ExtraFieldSpec"` 的引号写法就是这个用途）。

- [ ] **Step 4: 跑测试确认通过（neo4j_client 层）**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_neo4j_client.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 写失败的测试（admin_ontology_routes 层）**

在 `tests/api/test_admin_ontology_routes.py` 找到 `create_term_type_category`/`update_term_type_category` 相关的现有测试区域，追加（按该文件已有的 FastAPI TestClient/fixture 写法接入，`graph_client` 依赖用该文件已有的 override 方式打桩）：

```python
async def test_create_term_type_ensures_neo4j_indexes_for_declared_fields(client, override_graph_client):
    response = client.post(
        "/api/admin/ontology/muji/term-types",
        json={
            "value": "Product",
            "extra_fields": [{"name": "numeric_value", "value_type": "number"}],
            "node_key_template": "",
        },
        headers=_ADMIN_HEADERS,
    )
    assert response.status_code == 200
    assert override_graph_client.ensured_index_calls == [
        ("muji", "Product", [("numeric_value", "number")])
    ]
```

（`override_graph_client`/`_ADMIN_HEADERS`/`client` 三个 fixture 名字按该测试文件已有的具体命名接，如果该文件目前打桩 `graph_client` 的方式是一个简单的 fake 类而不是 fixture，就照抄同一种打桩方式，给这个 fake 类新增一个 `ensured_index_calls` 列表属性和 `ensure_extra_field_indexes` 方法记录调用参数，不要另起一套新的打桩机制。）

- [ ] **Step 6: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_admin_ontology_routes.py -k ensures_neo4j_indexes -v`
Expected: FAIL（路由目前不调用 `ensure_extra_field_indexes`，也没有注入 `graph_client`）。

- [ ] **Step 7: 实现（admin_ontology_routes.py）**

`app/api/admin_ontology_routes.py` 的 `create_term_type_category`/`update_term_type_category` 两个路由函数，加上 `graph_client` 依赖，在 SQLite 写入成功后调用新方法：

```python
@router.post("/{tenant_id}/term-types")
async def create_term_type_category(
    tenant_id: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict:
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await create_term_type(
            review_conn, tenant_id, value=payload.value,
            extra_fields=extra_field_specs,
            node_key_template=payload.node_key_template,
        )
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await graph_client.ensure_extra_field_indexes(
        tenant_id=tenant_id, term_type=payload.value, extra_fields=extra_field_specs,
    )
    return payload.model_dump()


@router.put("/{tenant_id}/term-types/{value}")
async def update_term_type_category(
    tenant_id: str,
    value: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict:
    extra_field_specs = _to_extra_field_specs(payload.extra_fields)
    try:
        await update_term_type(
            review_conn, tenant_id, value=value, new_value=payload.value,
            extra_fields=extra_field_specs,
            node_key_template=payload.node_key_template,
        )
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="分类不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidExtraFieldTypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await graph_client.ensure_extra_field_indexes(
        tenant_id=tenant_id, term_type=payload.value, extra_fields=extra_field_specs,
    )
    return payload.model_dump()
```

（改名场景下 `payload.value` 是新名字——索引按新的 `term_type` 名字建，旧名字下的索引不清理，Neo4j `CREATE INDEX IF NOT EXISTS` 本身也不做"删除不再需要的索引"这件事，属于可接受的现状，跟 ETL 写入引擎"只 MERGE 不删除"的既定原则一致，不在本任务范围内处理。）

- [ ] **Step 8: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_admin_ontology_routes.py -v`
Expected: 全部 PASS。

- [ ] **Step 9: 提交**

```bash
git add app/graphrag/neo4j_client.py app/api/admin_ontology_routes.py tests/graphrag/test_neo4j_client.py tests/api/test_admin_ontology_routes.py
git commit -m "feat(graphrag): create Neo4j indexes for confirmed scalar extra fields on term-type declare"
```

---

### Task 3: `structured_filter_query.py` —— 参数解析 + schema 校验链

**Files:**
- Create: `app/graphrag/structured_filter_query.py`
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Consumes: `app.graphrag.ontology_categories.TermTypeCategory(value, extra_fields, node_key_template)`。
- Produces：
  - `Hop(relation_type: str, direction: str, target_term_type: str)`
  - `AttributeConstraint(field: str, operator: str, value: object)`
  - `RelationConstraint(hops: list[Hop], target_field: str, target_operator: str, target_value: object)`
  - `GroupBy(constraint_index: int)`
  - `StructuredFilterQueryArgs(anchor_term_type: str, constraints: list[AttributeConstraint | RelationConstraint], group_by: GroupBy | None, limit: int)`
  - `StructuredFilterQueryError(Exception)`
  - `parse_structured_filter_query_args(raw: dict) -> StructuredFilterQueryArgs`
  - `validate_structured_filter_query(args: StructuredFilterQueryArgs, *, confirmed_relation_types: set[str], term_type_schema: dict[str, TermTypeCategory]) -> None`

  这四个（含两个函数）供 Task 5 的 `run_structured_filter_query` 编排调用；`Hop`/`AttributeConstraint`/`RelationConstraint`/`GroupBy`/`StructuredFilterQueryArgs` 也供 Task 4 的 `Neo4jGraphClient.execute_structured_filter_query` 作为入参类型。

- [ ] **Step 1: 写失败的测试**

创建 `tests/graphrag/test_structured_filter_query.py`：

```python
from __future__ import annotations

import pytest

from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.structured_filter_query import (
    AttributeConstraint,
    GroupBy,
    Hop,
    RelationConstraint,
    StructuredFilterQueryError,
    parse_structured_filter_query_args,
    validate_structured_filter_query,
)


def test_parse_attribute_constraint():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    assert args.anchor_term_type == "SKU"
    assert args.constraints == [AttributeConstraint(field="numeric_value", operator="gt", value=500)]
    assert args.group_by is None
    assert args.limit == 20


def test_parse_relation_constraint_with_two_hops():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [
                {"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"},
            ],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    constraint = args.constraints[0]
    assert isinstance(constraint, RelationConstraint)
    assert constraint.hops == [Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")]
    assert constraint.target_field == "raw_value"


def test_parse_rejects_empty_constraints():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor_term_type": "SKU", "constraints": []})


def test_parse_rejects_more_than_two_hops():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{
                "kind": "relation",
                "hops": [
                    {"relation_type": "A", "direction": "outgoing", "target_term_type": "X"},
                    {"relation_type": "B", "direction": "outgoing", "target_term_type": "Y"},
                    {"relation_type": "C", "direction": "outgoing", "target_term_type": "Z"},
                ],
                "target_field": "f", "target_operator": "eq", "target_value": "v",
            }],
        })


def test_parse_rejects_unknown_operator():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{"kind": "attribute", "field": "x", "operator": "contains", "value": "y"}],
        })


def test_parse_group_by_must_point_to_relation_constraint():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
            "group_by": {"constraint_index": 0},
        })


def test_parse_uses_default_limit_when_omitted():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    assert args.limit == 20


_SKU_SCHEMA = TermTypeCategory(
    value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    node_key_template="",
)
_VARIANT_SCHEMA = TermTypeCategory(
    value="VariantValue", extra_fields=[ExtraFieldSpec(name="raw_value", value_type="string")],
    node_key_template="",
)


def test_validate_accepts_confirmed_field_and_matching_operator():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA},
    )  # 不抛异常即通过


def test_validate_rejects_field_not_in_confirmed_schema():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_rejects_operator_not_matching_declared_value_type():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "starts_with", "value": "5"}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_accepts_standard_name_as_reserved_field():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "starts_with", "value": "圆角"}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )


def test_validate_rejects_relation_type_not_confirmed():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(),  # 空集合，HAS_VARIANT 未确认
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )


def test_validate_accepts_confirmed_relation_type_and_target_field():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types={"HAS_VARIANT"},
        term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
    )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_structured_filter_query.py -v`
Expected: FAIL（`app.graphrag.structured_filter_query` 模块不存在）。

- [ ] **Step 3: 实现**

创建 `app/graphrag/structured_filter_query.py`：

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from app.graphrag.ontology_categories import TermTypeCategory

# 与 neo4j_client.py::_RELATION_TYPE_NAME_PATTERN 保持同一份格式约束（有意重复定义，
# 不做跨模块导入——两处校验的是同一条注入防线契约，但分属"解析请求参数"和"拼
# Cypher"两个不同职责层，各自独立演化不构成重复劳动，见 docs/superpowers/specs/
# 2026-08-17-structured-filter-query-tool-design.md 第4节）。
_RELATION_TYPE_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")

_MAX_HOPS = 2
_RESERVED_FIELD_NAME = "standard_name"

_STRING_OPERATORS = frozenset({"eq", "ne", "starts_with"})
_NUMERIC_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq", "ne"})
_ARRAY_OPERATORS = frozenset({"all_lte", "all_gte", "any_lte", "any_gte"})
_VALID_OPERATORS = _STRING_OPERATORS | _NUMERIC_OPERATORS | _ARRAY_OPERATORS
_OPERATORS_BY_VALUE_TYPE = {
    "string": _STRING_OPERATORS,
    "number": _NUMERIC_OPERATORS,
    "integer": _NUMERIC_OPERATORS,
    "number[]": _ARRAY_OPERATORS,
}
_VALID_KINDS = frozenset({"attribute", "relation"})


class StructuredFilterQueryError(Exception):
    """请求参数没通过解析或 schema 校验链——字段/关系类型不在已确认 schema 里、
    运算符和字段声明的类型不匹配、hops 超过2跳等。调用方（本次改造的
    app/agent/tools.py::structured_filter_query_tool）捕获这个异常，转成结构化
    {"error": ...} 观察结果返回给 LLM，不让它作为未处理异常向上传播——见
    docs/superpowers/specs/2026-08-17-structured-filter-query-tool-design.md 第4节。"""


@dataclass(frozen=True)
class Hop:
    relation_type: str
    direction: str
    target_term_type: str


@dataclass(frozen=True)
class AttributeConstraint:
    field: str
    operator: str
    value: object


@dataclass(frozen=True)
class RelationConstraint:
    hops: list[Hop]
    target_field: str
    target_operator: str
    target_value: object


@dataclass(frozen=True)
class GroupBy:
    constraint_index: int


@dataclass(frozen=True)
class StructuredFilterQueryArgs:
    anchor_term_type: str
    constraints: list[AttributeConstraint | RelationConstraint]
    group_by: GroupBy | None
    limit: int


def _parse_hop(raw: dict) -> Hop:
    try:
        relation_type = raw["relation_type"]
        direction = raw["direction"]
        target_term_type = raw["target_term_type"]
    except KeyError as exc:
        raise StructuredFilterQueryError(f"hop 缺少必填字段: {exc}") from exc
    if direction not in ("outgoing", "incoming"):
        raise StructuredFilterQueryError(f"hop.direction 必须是 outgoing/incoming，收到: {direction!r}")
    return Hop(relation_type=relation_type, direction=direction, target_term_type=target_term_type)


def _parse_constraint(raw: dict) -> AttributeConstraint | RelationConstraint:
    kind = raw.get("kind")
    if kind not in _VALID_KINDS:
        raise StructuredFilterQueryError(f"constraint.kind 必须是 attribute/relation，收到: {kind!r}")
    if kind == "attribute":
        try:
            field = raw["field"]
            operator = raw["operator"]
            value = raw["value"]
        except KeyError as exc:
            raise StructuredFilterQueryError(f"attribute 约束缺少必填字段: {exc}") from exc
        if operator not in _VALID_OPERATORS:
            raise StructuredFilterQueryError(f"不支持的 operator: {operator!r}")
        return AttributeConstraint(field=field, operator=operator, value=value)
    try:
        raw_hops = raw["hops"]
        target_field = raw["target_field"]
        target_operator = raw["target_operator"]
        target_value = raw["target_value"]
    except KeyError as exc:
        raise StructuredFilterQueryError(f"relation 约束缺少必填字段: {exc}") from exc
    if not raw_hops:
        raise StructuredFilterQueryError("relation 约束的 hops 不能为空")
    if len(raw_hops) > _MAX_HOPS:
        raise StructuredFilterQueryError(f"hops 最多 {_MAX_HOPS} 跳，收到 {len(raw_hops)} 跳")
    if target_operator not in _VALID_OPERATORS:
        raise StructuredFilterQueryError(f"不支持的 target_operator: {target_operator!r}")
    hops = [_parse_hop(h) for h in raw_hops]
    return RelationConstraint(
        hops=hops, target_field=target_field, target_operator=target_operator, target_value=target_value,
    )


def _parse_group_by(raw: dict | None, *, constraints: list[AttributeConstraint | RelationConstraint]) -> GroupBy | None:
    if raw is None:
        return None
    try:
        constraint_index = raw["constraint_index"]
    except KeyError as exc:
        raise StructuredFilterQueryError(f"group_by 缺少必填字段: {exc}") from exc
    if constraint_index < 0 or constraint_index >= len(constraints):
        raise StructuredFilterQueryError(f"group_by.constraint_index {constraint_index} 越界")
    if not isinstance(constraints[constraint_index], RelationConstraint):
        raise StructuredFilterQueryError("group_by.constraint_index 必须指向一个 relation 约束")
    return GroupBy(constraint_index=constraint_index)


def parse_structured_filter_query_args(raw: dict) -> StructuredFilterQueryArgs:
    """把 LLM 工具调用传来的原始 JSON dict 解析成结构化参数——只做形状校验（必填
    字段是否存在、hops 跳数、operator 是否在协议允许的枚举里），不查 schema 是否
    真的已确认，那是 validate_structured_filter_query 的职责（需要 confirmed_
    relation_types/term_type_schema 这两份数据，本函数没有）。"""
    try:
        anchor_term_type = raw["anchor_term_type"]
        raw_constraints = raw["constraints"]
    except KeyError as exc:
        raise StructuredFilterQueryError(f"缺少必填字段: {exc}") from exc
    if not raw_constraints:
        raise StructuredFilterQueryError("constraints 不能为空，至少提供一个过滤条件")
    constraints = [_parse_constraint(c) for c in raw_constraints]
    group_by = _parse_group_by(raw.get("group_by"), constraints=constraints)
    limit = raw.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise StructuredFilterQueryError(f"limit 必须是正整数，收到: {limit!r}")
    return StructuredFilterQueryArgs(
        anchor_term_type=anchor_term_type, constraints=constraints, group_by=group_by, limit=limit,
    )


def _resolve_field_value_type(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str:
    if field == _RESERVED_FIELD_NAME:
        return "string"
    category = term_type_schema.get(term_type)
    if category is None:
        raise StructuredFilterQueryError(f"term_type {term_type!r} 不在已确认 schema 里")
    for spec in category.extra_fields:
        if spec.name == field:
            return spec.value_type
    raise StructuredFilterQueryError(f"字段 {field!r} 不是 {term_type!r} 已确认的属性字段")


def _validate_operator_for_value_type(*, field: str, operator: str, value_type: str) -> None:
    allowed = _OPERATORS_BY_VALUE_TYPE[value_type]
    if operator not in allowed:
        raise StructuredFilterQueryError(
            f"字段 {field!r}（类型 {value_type!r}）不支持运算符 {operator!r}，可用运算符: {sorted(allowed)}"
        )


def validate_structured_filter_query(
    args: StructuredFilterQueryArgs,
    *,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> None:
    """schema 层面的校验——anchor/target 的 term_type、field、relation_type 是否
    真的在这个租户已确认的 schema 里，operator 是否匹配字段声明的 value_type。
    形状层面的校验（必填字段、跳数上限等）由 parse_structured_filter_query_args
    完成，不在这里重复。"""
    if args.anchor_term_type not in term_type_schema:
        raise StructuredFilterQueryError(f"anchor_term_type {args.anchor_term_type!r} 不在已确认 schema 里")
    for constraint in args.constraints:
        if isinstance(constraint, AttributeConstraint):
            value_type = _resolve_field_value_type(
                term_type=args.anchor_term_type, field=constraint.field, term_type_schema=term_type_schema,
            )
            _validate_operator_for_value_type(field=constraint.field, operator=constraint.operator, value_type=value_type)
            continue
        for hop in constraint.hops:
            if not _RELATION_TYPE_NAME_PATTERN.match(hop.relation_type):
                raise StructuredFilterQueryError(f"关系类型名字不合法: {hop.relation_type!r}")
            if hop.relation_type not in confirmed_relation_types:
                raise StructuredFilterQueryError(f"relation_type {hop.relation_type!r} 不在已确认 schema 里")
            if hop.target_term_type not in term_type_schema:
                raise StructuredFilterQueryError(f"target_term_type {hop.target_term_type!r} 不在已确认 schema 里")
        last_hop = constraint.hops[-1]
        value_type = _resolve_field_value_type(
            term_type=last_hop.target_term_type, field=constraint.target_field, term_type_schema=term_type_schema,
        )
        _validate_operator_for_value_type(
            field=constraint.target_field, operator=constraint.target_operator, value_type=value_type,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_structured_filter_query.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "feat(graphrag): parse and validate structured filter query args against confirmed schema"
```

---

### Task 4: `Neo4jGraphClient.execute_structured_filter_query` —— Cypher 构造+执行

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Consumes: Task 3 的 `StructuredFilterQueryArgs`/`AttributeConstraint`/`RelationConstraint`/`Hop`/`GroupBy`（已通过校验，本任务不重复校验）。
- Produces: `Neo4jGraphClient.execute_structured_filter_query(self, args: StructuredFilterQueryArgs, *, tenant_id: str) -> list[dict[str, Any]] | dict[str, Any]`，非 `group_by` 时返回原始行列表（每行含 `standard_name`/`node_key`/`term_type`/`product_line`/`all_properties`），`group_by` 时返回 `{"groups": [{"value": ..., "count": ...}]}`——供 Task 5 的编排函数消费、格式化成最终返回给 LLM 的 JSON。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_neo4j_client.py` 追加：

```python
async def test_execute_structured_filter_query_builds_attribute_where_clause():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[
        {"standard_name": "SKU A", "node_key": "SKU:1", "term_type": "SKU",
         "product_line": "MUJI", "all_properties": {"numeric_value": 600}},
    ])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="SKU",
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        group_by=None, limit=20,
    )

    result = await client.execute_structured_filter_query(args, tenant_id="muji")

    assert result == [
        {"standard_name": "SKU A", "node_key": "SKU:1", "term_type": "SKU",
         "product_line": "MUJI", "all_properties": {"numeric_value": 600}},
    ]
    assert session.last_parameters["tenant_id"] == "muji"
    assert session.last_parameters["anchor_term_type"] == "SKU"
    assert session.last_parameters["field_0"] == "numeric_value"
    assert session.last_parameters["value_0"] == 500
    assert session.last_parameters["limit"] == 20
    assert "anchor[$field_0]" in session.last_query
    assert "> $value_0" in session.last_query


async def test_execute_structured_filter_query_builds_relation_exists_subquery():
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="SKU",
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
            target_field="raw_value", target_operator="eq", target_value="红",
        )],
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(args, tenant_id="muji")

    assert "EXISTS {" in session.last_query
    assert "-[:HAS_VARIANT]->" in session.last_query
    assert session.last_parameters["c0_target_field"] == "raw_value"
    assert session.last_parameters["c0_target_value"] == "红"


async def test_execute_structured_filter_query_incoming_direction_reverses_arrow():
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="VariantValue",
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="incoming", target_term_type="SKU")],
            target_field="price", target_operator="gt", target_value=0,
        )],
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(args, tenant_id="muji")

    assert "<-[:HAS_VARIANT]-" in session.last_query


async def test_execute_structured_filter_query_group_by_returns_aggregated_groups():
    from app.graphrag.structured_filter_query import GroupBy, Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[{"value": "红色", "count": 12}, {"value": "白色", "count": 8}])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="SKU",
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
            target_field="raw_value", target_operator="eq", target_value="__group__",
        )],
        group_by=GroupBy(constraint_index=0), limit=20,
    )

    result = await client.execute_structured_filter_query(args, tenant_id="muji")

    assert result == {"groups": [{"value": "红色", "count": 12}, {"value": "白色", "count": 8}]}
    assert "count(DISTINCT anchor)" in session.last_query


async def test_execute_structured_filter_query_array_operator_uses_list_predicate():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor_term_type="SKU",
        constraints=[AttributeConstraint(field="dims", operator="all_lte", value=80)],
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(args, tenant_id="muji")

    assert "all(x IN anchor[$field_0] WHERE x <= $value_0)" in session.last_query
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_neo4j_client.py -k execute_structured_filter_query -v`
Expected: FAIL（方法不存在）。

- [ ] **Step 3: 实现**

在 `app/graphrag/neo4j_client.py` 文件顶部 import 区新增：

```python
from app.graphrag.structured_filter_query import (
    AttributeConstraint,
    Hop,
    RelationConstraint,
    StructuredFilterQueryArgs,
)
```

在文件里（`_ALLOWED_RELATION_TYPES` 已经在今天的修复轮删掉，模块顶部现在是 `_RESERVED_RELATION_TYPES`/`_RELATION_TYPE_NAME_PATTERN`，在这些常量附近）新增运算符映射表：

```python
_COMPARISON_OPERATOR_TO_CYPHER = {
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "<>",
}


def _comparison_expression(*, prop_expr: str, operator: str, param_name: str) -> str:
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


def _build_hop_match_pattern(hops: list[Hop], *, prefix: str) -> tuple[str, dict[str, object]]:
    params: dict[str, object] = {}
    pattern = "MATCH (anchor)"
    for i, hop in enumerate(hops):
        var = f"{prefix}_hop{i}"
        type_param = f"{prefix}_type{i}"
        params[type_param] = hop.target_term_type
        arrow = f"-[:{hop.relation_type}]->" if hop.direction == "outgoing" else f"<-[:{hop.relation_type}]-"
        pattern += f"{arrow}({var}:Term {{tenant_id: $tenant_id, type: ${type_param}}})"
    return pattern, params
```

在 `Neo4jGraphClient` 类里新增方法（放在 `query_subgraph` 附近，同属"读查询"分组）：

```python
    async def execute_structured_filter_query(
        self, args: StructuredFilterQueryArgs, *, tenant_id: str
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """按已校验的结构化条件筛选 Term 节点——调用方（app/graphrag/
        structured_filter_query.py::run_structured_filter_query）必须已经跑过
        validate_structured_filter_query，本方法不重复校验 field/relation_type
        是否在已确认 schema 里，只负责构造 Cypher 并执行。

        属性字段名（field/target_field）一律走 t[$param] 动态属性访问，不做字符串
        插值——relation_type 做不到参数化（Cypher 关系类型语法层面要求字面量），
        是本方法里唯一需要字符串插值拼进查询文本的部分，安全性依赖调用方已经过
        validate_structured_filter_query 的格式+已确认成员双重校验。见
        docs/superpowers/specs/2026-08-17-structured-filter-query-tool-design.md
        第5节。
        """
        params: dict[str, Any] = {"tenant_id": tenant_id, "anchor_term_type": args.anchor_term_type}
        where_clauses: list[str] = []

        for i, constraint in enumerate(args.constraints):
            if isinstance(constraint, AttributeConstraint):
                field_param, value_param = f"field_{i}", f"value_{i}"
                params[field_param] = constraint.field
                params[value_param] = constraint.value
                where_clauses.append(
                    _comparison_expression(
                        prop_expr=f"anchor[${field_param}]", operator=constraint.operator, param_name=value_param,
                    )
                )
                continue
            if args.group_by is not None and args.group_by.constraint_index == i:
                continue  # group_by 指向的约束走独立的 MATCH（下方分支），不进 EXISTS
            match_pattern, hop_params = _build_hop_match_pattern(constraint.hops, prefix=f"c{i}")
            params.update(hop_params)
            target_field_param, target_value_param = f"c{i}_target_field", f"c{i}_target_value"
            params[target_field_param] = constraint.target_field
            params[target_value_param] = constraint.target_value
            last_var = f"c{i}_hop{len(constraint.hops) - 1}"
            comparison = _comparison_expression(
                prop_expr=f"{last_var}[${target_field_param}]",
                operator=constraint.target_operator, param_name=target_value_param,
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
            params["group_field"] = group_constraint.target_field
            last_var = f"g{args.group_by.constraint_index}_hop{len(group_constraint.hops) - 1}"
            query = (
                "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type}) "
                f"{match_pattern} "
                f"WHERE {where_sql} "
                f"RETURN {last_var}[$group_field] AS value, count(DISTINCT anchor) AS count "
                "ORDER BY count DESC"
            )
            async with self._driver.session() as session:
                result = await session.run(query, params)
                rows = await result.data()
            return {"groups": rows}

        params["limit"] = args.limit
        query = (
            "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type}) "
            f"WHERE {where_sql} "
            "RETURN anchor.standard_name AS standard_name, anchor.node_key AS node_key, "
            "anchor.type AS term_type, anchor.product_line AS product_line, "
            "properties(anchor) AS all_properties "
            "LIMIT $limit"
        )
        async with self._driver.session() as session:
            result = await session.run(query, params)
            rows = await result.data()
        return rows
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_neo4j_client.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(graphrag): build and execute structured filter query Cypher against validated args"
```

---

### Task 5: `structured_filter_query.py` 编排入口 —— 校验+执行+返回形状

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Consumes: Task 3 的 `parse_structured_filter_query_args`/`validate_structured_filter_query`/`StructuredFilterQueryError`；Task 4 的 `Neo4jGraphClient.execute_structured_filter_query`。
- Produces: `async def run_structured_filter_query(raw_args: dict, *, graph_client: Neo4jGraphClient, tenant_id: str, confirmed_relation_types: set[str], term_type_schema: dict[str, TermTypeCategory]) -> dict[str, Any]`，供 Task 6 的 `app/agent/tools.py::structured_filter_query_tool` 直接调用；返回值就是要塞进 LLM 观察结果的 JSON 内容（不用再包一层）。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_structured_filter_query.py` 追加（复用 `_SKU_SCHEMA`/`_VARIANT_SCHEMA`）：

```python
class _FakeGraphClient:
    def __init__(self, *, rows=None, group_result=None) -> None:
        self._rows = rows if rows is not None else []
        self._group_result = group_result
        self.last_args = None
        self.last_tenant_id = None

    async def execute_structured_filter_query(self, args, *, tenant_id):
        self.last_args = args
        self.last_tenant_id = tenant_id
        if self._group_result is not None:
            return self._group_result
        return self._rows


async def test_run_structured_filter_query_returns_error_on_invalid_args():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU", "constraints": []},
        graph_client=_FakeGraphClient(), tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "error" in result


async def test_run_structured_filter_query_returns_error_on_unconfirmed_field():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}]},
        graph_client=_FakeGraphClient(), tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "error" in result


async def test_run_structured_filter_query_formats_matched_results():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
         "product_line": "MUJI", "all_properties": {
             "tenant_id": "muji", "node_key": "SKU:1", "standard_name": "圆角收纳盒 500ml",
             "type": "SKU", "product_line": "MUJI", "numeric_value": 600,
         }},
    ])

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result["matched_count"] == 1
    assert result["results"] == [{
        "standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1",
        "term_type": "SKU", "product_line": "MUJI",
        "extra_properties": {"numeric_value": 600},
    }]
    assert graph_client.last_tenant_id == "muji"


async def test_run_structured_filter_query_passes_through_group_by_result():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(group_result={"groups": [{"value": "红色", "count": 12}]})

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{
             "kind": "relation",
             "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
             "target_field": "raw_value", "target_operator": "eq", "target_value": "__group__",
         }],
         "group_by": {"constraint_index": 0}},
        graph_client=graph_client, tenant_id="muji",
        confirmed_relation_types={"HAS_VARIANT"},
        term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
    )

    assert result == {"groups": [{"value": "红色", "count": 12}]}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_structured_filter_query.py -k run_structured_filter_query -v`
Expected: FAIL（`run_structured_filter_query` 不存在）。

- [ ] **Step 3: 实现**

在 `app/graphrag/structured_filter_query.py` 顶部 import 区新增（注意：这会让本模块反过来被 `neo4j_client.py` 导入，`neo4j_client.py` 不能再导入回本模块的其它符号形成循环——Task 4 已经确认只导入了 dataclass 类型，没有循环）：

```python
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.graphrag.neo4j_client import Neo4jGraphClient
```

在文件末尾新增：

```python
_CORE_TERM_FIELDS = frozenset({"tenant_id", "node_key", "standard_name", "type", "product_line"})


async def run_structured_filter_query(
    raw_args: dict,
    *,
    graph_client: "Neo4jGraphClient",
    tenant_id: str,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> dict[str, Any]:
    """structured_filter_query_tool 的执行体调用的编排入口：解析→校验→执行→格式化，
    四步都在这一个函数里完成，调用方（app/agent/tools.py）不需要知道 Task 3/4 拆出
    的中间函数名字。"""
    try:
        args = parse_structured_filter_query_args(raw_args)
        validate_structured_filter_query(
            args, confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}

    result = await graph_client.execute_structured_filter_query(args, tenant_id=tenant_id)
    if isinstance(result, dict):
        return result  # group_by 分支已经是 {"groups": [...]}

    return {
        "matched_count": len(result),
        "results": [
            {
                "standard_name": row["standard_name"],
                "node_key": row["node_key"],
                "term_type": row["term_type"],
                "product_line": row["product_line"],
                "extra_properties": {
                    k: v for k, v in row["all_properties"].items() if k not in _CORE_TERM_FIELDS
                },
            }
            for row in result
        ],
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_structured_filter_query.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "feat(graphrag): orchestrate structured filter query parse/validate/execute/format"
```

---

### Task 6: Agent 工具接入 —— schema 预加载 + 工具注册 + 分发

**Files:**
- Modify: `app/api/deps.py`
- Modify: `app/agent/tools.py`
- Modify: `app/agent/planner.py`
- Modify: `app/agent/graph.py`
- Modify: `app/api/agent_routes.py`
- Test: `tests/api/test_deps.py`
- Test: `tests/agent/test_tools.py`
- Test: `tests/agent/test_planner.py`

**Interfaces:**
- Consumes: Task 5 的 `run_structured_filter_query`；`app.graphrag.ontology_relations.list_relation_types(conn, tenant_id, *, status)`；`app.graphrag.ontology_categories.list_term_types(conn, tenant_id)`。
- Produces: `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA`（OpenAI function-calling schema）+ `structured_filter_query_tool(...)` 执行体（`app/agent/tools.py`）；`app/api/deps.py::get_confirmed_relation_types`/`get_term_type_schema` 两个新 FastAPI 依赖，跟 `get_terms` 一样按请求预加载一次。

- [ ] **Step 1: 写失败的测试（deps.py 两个新依赖）**

在 `tests/api/test_deps.py` 找到 `get_terms` 相关的现有测试区域，追加（按该文件已有的 conn 打桩/fixture 写法接）：

```python
async def test_get_confirmed_relation_types_returns_only_confirmed_types(review_conn):
    from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology
    from app.graphrag.ontology_relations import create_relation_type

    await checkout_draft(review_conn, "muji")
    await create_relation_type(review_conn, "muji", relation_type="HAS_SKU", example_phrase="p")
    await confirm_ontology(review_conn, "muji")

    result = await deps.get_confirmed_relation_types(
        review_conn=review_conn, gateway_tenant_id="muji",
    )

    assert result == {"HAS_SKU"}


async def test_get_term_type_schema_returns_dict_keyed_by_value(review_conn):
    from app.graphrag.ontology_categories import create_term_type

    await create_term_type(review_conn, tenant_id="muji", value="SKU")

    result = await deps.get_term_type_schema(review_conn=review_conn, gateway_tenant_id="muji")

    assert "SKU" in result
    assert result["SKU"].value == "SKU"
```

（`review_conn` fixture 名字按该测试文件已有的具体命名接，如果没有现成的就照抄 `get_terms` 测试用的连接构造方式。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_deps.py -k "confirmed_relation_types or term_type_schema" -v`
Expected: FAIL（两个函数不存在）。

- [ ] **Step 3: 实现（deps.py）**

在 `app/api/deps.py` 顶部 import 区新增：

```python
from app.graphrag.ontology_categories import TermTypeCategory, list_term_types
from app.graphrag.ontology_relations import list_relation_types
```

在 `get_terms` 函数之后新增（同一个"按请求预加载一次、不做进程级缓存"模式，`tenant_id` 解析方式跟 `get_terms` 完全一致，不重新发明）：

```python
async def get_confirmed_relation_types(
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
    gateway_tenant_id: str | None = Depends(get_gateway_tenant_id),
) -> set[str]:
    """结构化过滤查询工具校验 relation_type 用——跟 get_terms 一样，每次请求查一次，
    不做进程级缓存（租户在管理后台改关系类型是随时可能发生的事，缓存会导致查询
    工具用旧 schema 拒绝新确认的关系类型）。tenant_id 解析方式与 get_terms 保持
    完全一致，见该函数的说明。"""
    tenant_id = gateway_tenant_id or "default"
    defs = await list_relation_types(review_conn, tenant_id, status="confirmed")
    return {d.relation_type for d in defs}


async def get_term_type_schema(
    review_conn: aiosqlite.Connection = Depends(get_review_conn),
    gateway_tenant_id: str | None = Depends(get_gateway_tenant_id),
) -> dict[str, TermTypeCategory]:
    """结构化过滤查询工具校验 anchor_term_type/target_term_type/field 用。"""
    tenant_id = gateway_tenant_id or "default"
    categories = await list_term_types(review_conn, tenant_id)
    return {c.value: c for c in categories}
```

- [ ] **Step 4: 跑测试确认通过（deps.py）**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_deps.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 写失败的测试（tools.py + planner.py）**

在 `tests/agent/test_tools.py` 追加（复用文件已有的 `FakeGraphClient`，给它加一个 `execute_structured_filter_query` 方法，或者按该文件已有的 fake 扩展方式加一个新 fake 类——不要另起一套打桩机制）：

```python
async def test_structured_filter_query_tool_delegates_to_run_structured_filter_query():
    from app.agent.tools import structured_filter_query_tool
    from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, tenant_id):
            return []

    result = await structured_filter_query_tool(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        tenant_id="muji", graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
            node_key_template="",
        )},
    )

    assert result == {"matched_count": 0, "results": []}


def test_structured_filter_query_tool_schema_does_not_expose_tenant_id():
    from app.agent.tools import STRUCTURED_FILTER_QUERY_TOOL_SCHEMA
    properties = STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert "tenant_id" not in properties
```

在 `tests/agent/test_planner.py` 找到 `graph_query_tool`/`_dispatch_tool_call` 相关的现有测试区域，追加一个覆盖新工具分发路径的测试（按该文件已有的 state/fake 写法接，不重新设计测试基础设施）：

```python
async def test_dispatch_tool_call_routes_structured_filter_query_tool():
    from app.agent.planner import _dispatch_tool_call

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, tenant_id):
            return []

    content, records = await _dispatch_tool_call(
        "structured_filter_query_tool",
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        tenant_id="muji",
        embedding_registry=None, embedding_provider_name="", vector_store=None, bm25_index=None,
        llm_registry=None, llm_provider_name="", rerank_provider=None, query_rewrite_enabled=False,
        terms=None, graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={},
    )

    assert records == []
    assert "error" in content  # SKU 不在空的 term_type_schema 里，预期走结构化错误分支


async def test_dispatch_tool_call_reports_unconfigured_when_schema_data_missing():
    from app.agent.planner import _dispatch_tool_call

    content, records = await _dispatch_tool_call(
        "structured_filter_query_tool", {"anchor_term_type": "SKU", "constraints": []},
        tenant_id="muji",
        embedding_registry=None, embedding_provider_name="", vector_store=None, bm25_index=None,
        llm_registry=None, llm_provider_name="", rerank_provider=None, query_rewrite_enabled=False,
        terms=None, graph_client=None, confirmed_relation_types=None, term_type_schema=None,
    )

    assert records == []
    assert "未配置" in content
```

- [ ] **Step 6: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/agent/test_tools.py tests/agent/test_planner.py -k "structured_filter_query" -v`
Expected: FAIL（`structured_filter_query_tool`/`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 不存在，`_dispatch_tool_call` 不认识新参数/新工具名）。

- [ ] **Step 7: 实现（tools.py）**

在 `app/agent/tools.py` 顶部 import 区新增：

```python
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.structured_filter_query import run_structured_filter_query
```

`GRAPH_QUERY_TOOL_SCHEMA` 定义之后新增（完整 JSON Schema，按 spec 第3.1节原样抄）：

```python
STRUCTURED_FILTER_QUERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "structured_filter_query_tool",
        "description": (
            "按结构化条件（数值区间、精确匹配、关系约束）在知识图谱里筛选满足条件的实体，"
            "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」这类不知道具体"
            "实体名、需要按条件查找的问题。与 graph_query_tool 不同：graph_query_tool 用于"
            "已知实体名、查它的关联信息；本工具用于按条件反查一批满足条件的实体。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "anchor_term_type": {
                    "type": "string",
                    "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                },
                "constraints": {
                    "type": "array",
                    "description": "过滤条件列表，条件之间是 AND 关系，至少提供一个",
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
                "limit": {"type": "integer", "description": "返回结果的最大条数，默认20"},
            },
            "required": ["anchor_term_type", "constraints"],
        },
    },
}


async def structured_filter_query_tool(
    arguments: dict[str, Any],
    *,
    tenant_id: str,
    graph_client: GraphClientProtocol,
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
) -> dict[str, Any]:
    """structured_filter_query_tool 的实际执行体，薄封装
    structured_filter_query.py::run_structured_filter_query。"""
    return await run_structured_filter_query(
        arguments, graph_client=graph_client, tenant_id=tenant_id,
        confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
    )
```

`GraphClientProtocol`（`app/graphrag/term_guard.py`）新增一个方法声明，让类型检查认得这个新方法（不改变任何运行时行为，`Neo4jGraphClient` 本来就已经实现了它）：

```python
class GraphClientProtocol(Protocol):
    async def query_subgraph(
        self, node_key: str, *, tenant_id: str
    ) -> list[dict[str, Any]]: ...

    async def execute_structured_filter_query(
        self, args: Any, *, tenant_id: str
    ) -> list[dict[str, Any]] | dict[str, Any]: ...
```

- [ ] **Step 8: 实现（planner.py）**

`app/agent/planner.py` 顶部 import 区新增：

```python
from app.agent.tools import (
    GRAPH_QUERY_TOOL_SCHEMA,
    STRUCTURED_FILTER_QUERY_TOOL_SCHEMA,
    VECTOR_SEARCH_TOOL_SCHEMA,
    graph_query_tool,
    structured_filter_query_tool,
    vector_search_tool,
)
from app.graphrag.ontology_categories import TermTypeCategory
```

`_TOOL_SCHEMAS` 改成：

```python
_TOOL_SCHEMAS = [VECTOR_SEARCH_TOOL_SCHEMA, GRAPH_QUERY_TOOL_SCHEMA, STRUCTURED_FILTER_QUERY_TOOL_SCHEMA]
```

`_dispatch_tool_call` 签名新增两个参数（跟在 `graph_client` 后面），并追加新工具的分发分支：

```python
async def _dispatch_tool_call(
    name: str,
    arguments: dict[str, Any],
    *,
    tenant_id: str,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None,
    query_rewrite_enabled: bool,
    terms: list[Term] | None,
    graph_client: GraphClientProtocol | None,
    confirmed_relation_types: set[str] | None,
    term_type_schema: dict[str, TermTypeCategory] | None,
) -> tuple[str, list[VectorRecord]]:
    ...（vector_search_tool/graph_query_tool 两段不变，在 graph_query_tool 分支之后新增）

    if name == "structured_filter_query_tool":
        if graph_client is None or confirmed_relation_types is None or term_type_schema is None:
            return json.dumps({"error": "structured_filter_query_tool 未配置"}, ensure_ascii=False), []
        observation = await structured_filter_query_tool(
            arguments, tenant_id=tenant_id, graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
        return json.dumps(observation, ensure_ascii=False), []

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False), []
```

`run_tool_calls` 签名新增同样两个参数（默认值 `None`，跟 `terms`/`graph_client` 现有默认值模式一致），并在调用 `_dispatch_tool_call` 时透传：

```python
async def run_tool_calls(
    state: dict[str, Any],
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: GraphClientProtocol | None = None,
    confirmed_relation_types: set[str] | None = None,
    term_type_schema: dict[str, TermTypeCategory] | None = None,
) -> dict[str, Any]:
    ...
```

在 `_execute_one` 内部调用 `_dispatch_tool_call` 的地方，追加透传这两个新参数（紧跟 `graph_client=graph_client,` 之后）：

```python
            terms=terms,
            graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
        )
```

- [ ] **Step 9: 实现（graph.py + agent_routes.py）**

`app/agent/graph.py::build_agent_graph` 签名新增两个可选参数（跟 `terms`/`graph_client` 现有的 `| None = None` 模式一致）：

```python
    terms: list[Term] | None = None,
    graph_client: Neo4jGraphClient | None = None,
    confirmed_relation_types: set[str] | None = None,
    term_type_schema: dict[str, TermTypeCategory] | None = None,
```

（`TermTypeCategory` 的 import 加到该文件顶部 import 区：`from app.graphrag.ontology_categories import TermTypeCategory`，不需要字符串前向引用，直接导入即可——这个类型在 `graph.py` 里只做参数注解，不会造成循环导入。）

`tool_call_node` 闭包里调用 `run_tool_calls` 的地方追加透传：

```python
    async def tool_call_node(state: AgentState) -> dict[str, Any]:
        return await run_tool_calls(
            state,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
        )
```

`app/api/agent_routes.py` 的 `agent_chat_endpoint` 路由函数签名，追加两个新依赖（紧跟 `terms: list[Term] = Depends(deps.get_terms),` 之后）：

```python
    confirmed_relation_types: set[str] = Depends(deps.get_confirmed_relation_types),
    term_type_schema: dict = Depends(deps.get_term_type_schema),
```

调用 `build_agent_graph(...)` 的地方追加透传（紧跟 `terms=terms,` 之后）：

```python
            terms=terms,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
```

（`app/eval/runner.py` 里另外两处 `build_agent_graph(...)` 调用不需要改——新参数有默认值 `None`，`_dispatch_tool_call` 已经对 `None` 做了"未配置"降级处理，跟 `graph_client`/`terms` 目前在 eval 场景下的可选降级行为完全一致，不强制要求 eval 也接入这两份 schema 数据。）

- [ ] **Step 10: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/agent/test_tools.py tests/agent/test_planner.py tests/api/test_deps.py -v`
Expected: 全部 PASS。

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/agent/ tests/api/ -v`
Expected: 全部 PASS（确认没有破坏 `graph.py`/`agent_routes.py` 其它既有测试——两处新增参数都是可选默认值，不应该影响任何没有显式传它们的既有调用点）。

- [ ] **Step 11: 提交**

```bash
git add app/api/deps.py app/agent/tools.py app/agent/planner.py app/agent/graph.py app/api/agent_routes.py app/graphrag/term_guard.py tests/api/test_deps.py tests/agent/test_tools.py tests/agent/test_planner.py
git commit -m "feat(agent): register structured_filter_query_tool with pre-loaded confirmed-schema data"
```

---

## 完成后

全部6个任务完成后：跑一次全量回归（`pytest -q`），确认没有破坏任何既有测试；用 `superpowers:subagent-driven-development` 的标准流程做一次全分支终审，重点检查：Cypher 构造的 `EXISTS` 子查询语法是否与本仓库实际使用的 Neo4j 版本兼容（如果终审发现版本不兼容，这是本计划遗漏的一个真实前提假设，需要现场核实并调整第5节的查询语法）；`structured_filter_query_tool` 的工具描述文案（`description` 字段）在真实 LLM 调用下是否真的能让模型正确构造出 `constraints`/`hops` 这类嵌套结构（如果发现 LLM 经常构造错误，可能需要在 description 里补充一两个具体示例，这属于终审阶段才能发现的问题，不属于本计划任务范围内的单元测试能覆盖的东西）。
