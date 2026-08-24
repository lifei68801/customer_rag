# constraints 支持模糊解析 target_value 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `structured_filter_query_tool` 的 `constraints` 里针对 `standard_name` 字段的 `eq`/`ne` 比较值，复用 `anchor.name` 已经在用的 `resolve_term()` 做模糊/别名解析，让"coke-cola有多少个订单"这类问题第一次调用就能直接查出准确数字，不再依赖 LLM 记得先消歧、再发起第二次调用。

**Architecture:** 在 `run_structured_filter_query` 的"校验通过、执行之前"插入一个纯函数式的解析步骤，对 `constraints` 做不可变变换（`dataclasses.replace`），把 LLM 猜测的原始字符串替换成解析出的标准名。不改 Cypher 生成层、不改五层白名单校验本身。

**Tech Stack:** Python 3.12，pytest（异步测试，复用现有 `_FakeGraphClient`/`_COKE_TERM` 等测试替身）。

**Spec:** `docs/superpowers/specs/2026-08-24-fuzzy-constraint-value-resolution-design.md`

## Global Constraints

- 只对 `standard_name` 字段的 `eq`/`ne` 生效，且该字段这次必须被声明成字符串类型（`standard_name_value_type == "string"`）——数值类型的 `standard_name`（如"销量"）完全不受影响，行为跟改动前一致。
- 解析失败直接返回结构化错误（`StructuredFilterQueryError` → `{"error": ...}`），不做静默退回字面比较。
- 不改动 `app/graphrag/neo4j_client.py`（Cypher 生成层）、不改动 `validate_structured_filter_query`（五层白名单校验本身）、不改动 `app/agent/planner.py`（调度层）——这次改动完全收敛在 `structured_filter_query.py` 一个文件的编排逻辑里。
- 每个任务完成后运行改动涉及的测试文件，全绿再进入下一个任务。

---

### Task 1: 纯函数——`_should_fuzzy_resolve`/`_resolve_or_raise`/两个 `_maybe_resolve_*` 辅助函数

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Produces: `_should_fuzzy_resolve(*, field, operator, term_type, term_type_schema) -> bool`；`_resolve_or_raise(value, *, term_type, terms) -> str`（解析失败抛 `StructuredFilterQueryError`）；`_maybe_resolve_attribute_constraint(constraint, *, term_type, terms, term_type_schema) -> AttributeConstraint`；`_maybe_resolve_relation_constraint(constraint, *, terms, term_type_schema) -> RelationConstraint`。这几个是纯函数，不涉及 `run_structured_filter_query` 的编排逻辑，可以独立单测（Task 2 才会把它们接进编排流程）。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/graphrag/test_structured_filter_query.py 末尾

from app.graphrag.structured_filter_query import (
    _maybe_resolve_attribute_constraint,
    _maybe_resolve_relation_constraint,
    _resolve_or_raise,
    _should_fuzzy_resolve,
)

_COMPANY_SCHEMA_STRING = TermTypeCategory(value="公司", extra_fields=[])
_SALES_SCHEMA_NUMBER_FOR_FUZZY_TEST = TermTypeCategory(
    value="销量", extra_fields=[], standard_name_value_type="number",
)


def test_should_fuzzy_resolve_true_for_standard_name_eq_string_type():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="eq", term_type="公司",
        term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    ) is True


def test_should_fuzzy_resolve_true_for_standard_name_ne():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="ne", term_type="公司",
        term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    ) is True


def test_should_fuzzy_resolve_false_for_non_standard_name_field():
    assert _should_fuzzy_resolve(
        field="numeric_value", operator="eq", term_type="SKU",
        term_type_schema={"SKU": _SKU_SCHEMA},
    ) is False


def test_should_fuzzy_resolve_false_for_starts_with():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="starts_with", term_type="公司",
        term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    ) is False


def test_should_fuzzy_resolve_false_when_standard_name_declared_number():
    """销量这类 value-as-node 类型的 standard_name 是数值类型，eq 比较的是真实
    数字，不能被误当成实体名解析——这是本次改动最容易踩的回归点。"""
    assert _should_fuzzy_resolve(
        field="standard_name", operator="eq", term_type="销量",
        term_type_schema={"销量": _SALES_SCHEMA_NUMBER_FOR_FUZZY_TEST},
    ) is False


def test_should_fuzzy_resolve_false_for_unknown_term_type():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="eq", term_type="不存在的类型",
        term_type_schema={},
    ) is False


def test_resolve_or_raise_returns_standard_name_on_match():
    result = _resolve_or_raise("coke-cola", term_type="公司", terms=[_COKE_TERM])
    assert result == "Coca-Cola"


def test_resolve_or_raise_raises_when_not_found():
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        _resolve_or_raise("完全不认识的名字", term_type="公司", terms=[_COKE_TERM])
    message = str(exc_info.value)
    assert "完全不认识的名字" in message
    assert "公司" in message


def test_resolve_or_raise_raises_when_value_not_a_string():
    with pytest.raises(StructuredFilterQueryError):
        _resolve_or_raise(123, term_type="公司", terms=[_COKE_TERM])


def test_maybe_resolve_attribute_constraint_replaces_value_when_applicable():
    constraint = AttributeConstraint(field="standard_name", operator="eq", value="coke-cola")
    result = _maybe_resolve_attribute_constraint(
        constraint, term_type="公司", terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    )
    assert result == AttributeConstraint(field="standard_name", operator="eq", value="Coca-Cola")


def test_maybe_resolve_attribute_constraint_passes_through_when_not_applicable():
    constraint = AttributeConstraint(field="numeric_value", operator="gt", value=500)
    result = _maybe_resolve_attribute_constraint(
        constraint, term_type="SKU", terms=[], term_type_schema={"SKU": _SKU_SCHEMA},
    )
    assert result is constraint  # 原样透传，不是重新构造的等价对象


def test_maybe_resolve_attribute_constraint_raises_on_unresolvable_value():
    constraint = AttributeConstraint(field="standard_name", operator="eq", value="完全不认识的名字")
    with pytest.raises(StructuredFilterQueryError):
        _maybe_resolve_attribute_constraint(
            constraint, term_type="公司", terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
        )


def test_maybe_resolve_relation_constraint_replaces_target_value_when_applicable():
    constraint = RelationConstraint(
        hops=[Hop(relation_type="BELONG_TO", direction="outgoing", target_term_type="公司")],
        target_field="standard_name", target_operator="eq", target_value="coke-cola",
    )
    result = _maybe_resolve_relation_constraint(
        constraint, terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    )
    assert result.target_value == "Coca-Cola"
    assert result.hops == constraint.hops  # 其余字段不变


def test_maybe_resolve_relation_constraint_passes_through_when_not_applicable():
    constraint = RelationConstraint(
        hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
        target_field="raw_value", target_operator="starts_with", target_value="红",
    )
    result = _maybe_resolve_relation_constraint(
        constraint, terms=[], term_type_schema={"VariantValue": _VARIANT_SCHEMA},
    )
    assert result is constraint


def test_maybe_resolve_relation_constraint_uses_last_hop_type_for_two_hop_chain():
    """两跳约束要用最后一跳的 target_term_type 做解析类型提示，不是第一跳的。"""
    constraint = RelationConstraint(
        hops=[
            Hop(relation_type="BELONG_TO", direction="outgoing", target_term_type="产品"),
            Hop(relation_type="BELONG_TO", direction="outgoing", target_term_type="公司"),
        ],
        target_field="standard_name", target_operator="eq", target_value="coke-cola",
    )
    result = _maybe_resolve_relation_constraint(
        constraint, terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    )
    assert result.target_value == "Coca-Cola"
```

（顶部 import 区如果还没有 `AttributeConstraint`/`RelationConstraint`/`Hop`/`StructuredFilterQueryError`/`pytest` 这几个名字，补齐——文件里已有测试大概率已经导入了大部分，检查一下就好。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v -k "should_fuzzy_resolve or resolve_or_raise or maybe_resolve"`
Expected: 全部 FAIL（`ImportError`，这几个函数还不存在）。

- [ ] **Step 3: 实现**

在 `app/graphrag/structured_filter_query.py`：

1. 顶部导入区，把：
```python
from dataclasses import dataclass
```
改成：
```python
from dataclasses import dataclass, replace
```

2. 在 `validate_structured_filter_query` 函数定义结束之后、`_CORE_TERM_FIELDS = ...` 之前，新增：

```python
_FUZZY_RESOLVABLE_OPERATORS = frozenset({"eq", "ne"})


def _should_fuzzy_resolve(
    *, field: str, operator: str, term_type: str, term_type_schema: dict[str, TermTypeCategory]
) -> bool:
    if field != _RESERVED_FIELD_NAME or operator not in _FUZZY_RESOLVABLE_OPERATORS:
        return False
    category = term_type_schema.get(term_type)
    # term_type 此时已经过 validate_structured_filter_query 校验，category 必然存在；
    # 防御性写法不假设，None 时视为不满足模糊解析条件（走原有字面比较路径）。
    return category is not None and category.standard_name_value_type == "string"


def _resolve_or_raise(value: object, *, term_type: str, terms: list[Term]) -> str:
    if isinstance(value, str):
        term = resolve_term(value, terms, term_type_hint=term_type)
        if term is not None:
            return term.standard_name
    raise StructuredFilterQueryError(
        f"约束值 {value!r} 无法在术语表里解析成已确认的 {term_type!r} 类型实体，"
        f"请检查拼写，或先用 anchor.name 消歧确认准确的标准名称"
    )


def _maybe_resolve_attribute_constraint(
    constraint: AttributeConstraint,
    *,
    term_type: str,
    terms: list[Term],
    term_type_schema: dict[str, TermTypeCategory],
) -> AttributeConstraint:
    if not _should_fuzzy_resolve(
        field=constraint.field, operator=constraint.operator, term_type=term_type, term_type_schema=term_type_schema,
    ):
        return constraint
    resolved_value = _resolve_or_raise(constraint.value, term_type=term_type, terms=terms)
    return replace(constraint, value=resolved_value)


def _maybe_resolve_relation_constraint(
    constraint: RelationConstraint,
    *,
    terms: list[Term],
    term_type_schema: dict[str, TermTypeCategory],
) -> RelationConstraint:
    last_hop_type = constraint.hops[-1].target_term_type
    if not _should_fuzzy_resolve(
        field=constraint.target_field, operator=constraint.target_operator,
        term_type=last_hop_type, term_type_schema=term_type_schema,
    ):
        return constraint
    resolved_value = _resolve_or_raise(constraint.target_value, term_type=last_hop_type, terms=terms)
    return replace(constraint, target_value=resolved_value)
```

（`_resolve_fuzzy_constraint_values`——遍历整个 `constraints` 列表、调用这两个 `_maybe_resolve_*` 辅助函数的编排函数——是 Task 2 的职责，这一步不写，因为它要接进 `run_structured_filter_query`，属于下一个任务的范围。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v`
Expected: 全部 PASS，包括新增的这些纯函数测试和之前所有既有测试（这一步不改 `run_structured_filter_query`，既有的编排层测试不受影响）。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "feat(graphrag): add fuzzy-resolution helpers for standard_name constraint values"
```

---

### Task 2: 编排层——接入 `run_structured_filter_query`

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Consumes: Task 1 产出的 `_maybe_resolve_attribute_constraint`/`_maybe_resolve_relation_constraint`。
- Produces: 新增编排函数 `_resolve_fuzzy_constraint_values(constraints, *, anchor_term_type, terms, term_type_schema) -> list[AttributeConstraint | RelationConstraint]`；`run_structured_filter_query` 在校验通过之后、执行之前调用它，解析失败时提前返回 `{"error": ...}`。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/graphrag/test_structured_filter_query.py 末尾

async def test_run_structured_filter_query_resolves_fuzzy_relation_constraint_value():
    """核心场景：anchor.term_type + constraints 里用口语化别名（"coke-cola"），
    第一次调用就应该能查出正确结果——不需要先用 anchor.name 消歧再发第二次调用。"""
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "0-7380-9438-2", "node_key": "订单号:0-7380-9438-2",
         "term_type": "订单号", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单号"},
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "BELONG_TO", "direction": "outgoing", "target_term_type": "公司"}],
                "target_field": "standard_name", "target_operator": "eq", "target_value": "coke-cola",
            }],
        },
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types={"BELONG_TO"},
        term_type_schema={
            "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
            "公司": TermTypeCategory(value="公司", extra_fields=[]),
        },
    )

    assert result["matched_count"] == 1
    # 断言真正传给图数据库执行层的约束值，已经是解析后的标准名"Coca-Cola"，
    # 不是 LLM 原始猜测的"coke-cola"——这是本次改动要验证的核心行为。
    resolved_constraint = graph_client.last_args.constraints[0]
    assert resolved_constraint.target_value == "Coca-Cola"


async def test_run_structured_filter_query_resolves_fuzzy_attribute_constraint_value():
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "公司"},
            "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": "coke-cola"}],
        },
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(),
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )

    assert result["matched_count"] == 1
    assert graph_client.last_args.constraints[0].value == "Coca-Cola"


async def test_run_structured_filter_query_returns_error_and_skips_execution_when_constraint_value_unresolvable():
    class _ExplodingGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            raise AssertionError("约束值解析失败时不应该查图谱")

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单号"},
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "BELONG_TO", "direction": "outgoing", "target_term_type": "公司"}],
                "target_field": "standard_name", "target_operator": "eq", "target_value": "完全不认识的名字",
            }],
        },
        graph_client=_ExplodingGraphClient(), tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types={"BELONG_TO"},
        term_type_schema={
            "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
            "公司": TermTypeCategory(value="公司", extra_fields=[]),
        },
    )

    assert "error" in result
    assert "完全不认识的名字" in result["error"]


async def test_run_structured_filter_query_numeric_standard_name_eq_unaffected_by_fuzzy_resolution():
    """销量这类 value-as-node 类型的 standard_name 是数值——eq 比较数字时，
    完全不应该触发模糊解析，行为要跟本次改动之前一模一样（防回归）。"""
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "100", "node_key": "销量:100", "term_type": "销量", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "销量"},
            "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": 100}],
        },
        graph_client=graph_client, tenant_id="demo", terms=[],
        confirmed_relation_types=set(),
        term_type_schema={"销量": TermTypeCategory(
            value="销量", extra_fields=[], standard_name_value_type="number",
        )},
    )

    assert result["matched_count"] == 1
    # 数值 100 原样透传，不经过 _resolve_or_raise（terms=[] 也证明了这一点——
    # 如果误触发了模糊解析，空 terms 列表会导致解析失败报错，而不是正常返回结果）。
    assert graph_client.last_args.constraints[0].value == 100


async def test_run_structured_filter_query_name_anchor_constraints_also_resolve_fuzzy_values():
    """NameAnchor 模式下 constraints 里的模糊解析也要生效，不只是 TypeAnchor 模式。"""
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ])

    await run_structured_filter_query(
        {
            "anchor": {"name": "coke-cola"},
            "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": "coke-cola"}],
        },
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(),
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )

    assert graph_client.last_args.constraints[0].value == "Coca-Cola"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v -k "fuzzy"`
Expected: 新增的编排层测试 FAIL（`run_structured_filter_query` 还没有调用任何解析逻辑，`graph_client.last_args.constraints[0].target_value`/`.value` 仍然是原始未解析的字符串；解析失败的测试也不会走到 error 分支，因为压根没有解析这一步）。

- [ ] **Step 3: 实现**

在 `app/graphrag/structured_filter_query.py`，紧跟 Task 1 新增的两个 `_maybe_resolve_*` 函数之后，新增编排函数：

```python
def _resolve_fuzzy_constraint_values(
    constraints: list[AttributeConstraint | RelationConstraint],
    *,
    anchor_term_type: str,
    terms: list[Term],
    term_type_schema: dict[str, TermTypeCategory],
) -> list[AttributeConstraint | RelationConstraint]:
    """在 validate_structured_filter_query 通过之后调用：把 constraints 里针对
    standard_name 字段的 eq/ne 比较值，从 LLM 猜测的原始字符串解析成术语表里的
    标准名——跟 anchor.name 走的是同一套 resolve_term()，只是作用对象从"锚点
    自己的名字"扩展到"约束条件里引用的名字"，让"先消歧、再用消歧出的标准名
    发起第二次调用"这个两步流程能在一次调用里完成，见
    docs/superpowers/specs/2026-08-24-fuzzy-constraint-value-resolution-design.md。
    """
    resolved: list[AttributeConstraint | RelationConstraint] = []
    for constraint in constraints:
        if isinstance(constraint, AttributeConstraint):
            resolved.append(
                _maybe_resolve_attribute_constraint(
                    constraint, term_type=anchor_term_type, terms=terms, term_type_schema=term_type_schema,
                )
            )
            continue
        resolved.append(
            _maybe_resolve_relation_constraint(constraint, terms=terms, term_type_schema=term_type_schema)
        )
    return resolved
```

`run_structured_filter_query` 函数体里，找到：

```python
    try:
        validate_structured_filter_query(
            args, resolved=resolved,
            confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}

    # 执行阶段单独兜一层 except Exception（比 StructuredFilterQueryError 宽）——这层
```

在 `except StructuredFilterQueryError as exc: return {"error": str(exc)}` 和"执行阶段单独兜一层"那条注释之间，插入：

```python
    try:
        resolved_constraints = _resolve_fuzzy_constraint_values(
            args.constraints, anchor_term_type=resolved.term_type,
            terms=terms, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}
    args = replace(args, constraints=resolved_constraints)

```

（`args` 局部变量重新绑定成替换过 `constraints` 的新对象，后续 `graph_client.execute_structured_filter_query(args, ...)` 调用不用改——它读到的 `args` 已经是解析过的新对象。函数其余部分——包括结果格式化那一段——完全不变。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_structured_filter_query.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 运行 `tools.py`/`planner.py` 相关测试确认没有间接破坏**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py tests/agent/test_planner.py tests/agent/test_graph_planner.py -v`
Expected: 全部 PASS——这几个文件里用到 `run_structured_filter_query`/`structured_filter_query_tool` 的既有测试，传的 `constraints` 要么是空的、要么用的字段/值本来就不会命中 `standard_name`+`eq`/`ne` 这个组合，不应该受这次改动影响。如果发现有 FAIL，说明某个既有测试恰好落进了新逻辑的触发条件，需要回头检查该测试的 `terms`/`term_type_schema` 参数是否补全（模糊解析现在需要 `terms` 里真的有能匹配上的术语，否则会从"原样通过"变成"报错"）。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "feat(graphrag): resolve fuzzy standard_name values in constraints before execution"
```

---

### Task 3: 工具描述文案 + 系统提示词微调（可选内容，收尾）

**Files:**
- Modify: `app/agent/tools.py`
- Modify: `app/agent/graph.py`

**Interfaces:**
- 无新接口，纯文案改动，不影响任何已有测试的断言（除非某个测试恰好对这两处文案做了子串匹配，需要检查）。

- [ ] **Step 1: 检查既有测试是否对这两处文案做字符串匹配**

Run: `grep -rn "constraints" tests/agent/test_tools.py` 和 `grep -rn "通常需要两次调用\|matched_count" tests/agent/test_graph_planner.py tests/agent/test_graph.py`——确认没有测试断言这两处文案的具体字符串内容（大概率没有，参照本次会话里改 `_PLANNER_SYSTEM_PROMPT` 时的既有先例，这类测试只检查"存在 system 消息"，不检查具体文字）。如果发现有断言具体文案的测试，这一步先记录下来，Step 3 一并更新。

- [ ] **Step 2: 实现**

`app/agent/tools.py`，`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 的 `constraints` 字段 `description` 末尾追加一句（找到当前 `"description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，直接用解析出的锚点）"`，改成）：

```python
                "constraints": {
                    "type": "array",
                    "description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，"
                                   "直接用解析出的锚点）。standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
                                   "不要求填精确的标准名称——比如用户说的口语化名字可以直接填进来，系统会自动解析。",
```

（其余 `constraints` 的 schema 结构——`items` 里的字段定义——完全不变，只改这一处 `description` 字符串。）

`app/agent/graph.py`，`_PLANNER_SYSTEM_PROMPT` 里找到"先消歧、再筛选计数，通常需要两次调用"这句话（或类似措辞），改成弱化"两次调用是常态"这个暗示的说法——读取当前实际文本后，把这句话调整为类似"多数情况下一次调用就够（约束条件里可以直接填口语化的名字，系统会自动解析成标准名）；只有 anchor.name 消歧本身有歧义、需要先确认具体是哪个实体时，才需要先消歧、再用消歧结果发起第二次调用"这样的表述——保留原有的"消歧优先"逻辑，但不再暗示两次调用是默认路径。具体措辞根据读到的当前文本自然衔接，不要求逐字照抄这里给的示例句子。

- [ ] **Step 3: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tools.py tests/agent/test_graph_planner.py tests/agent/test_graph.py -v`
Expected: 全部 PASS。

- [ ] **Step 4: 提交**

```bash
git add app/agent/tools.py app/agent/graph.py
git commit -m "docs(agent): document constraints fuzzy-match support in tool schema and prompt"
```

---

## 最后（不在任何任务里，计划完成后由控制者手动执行）

用真实环境重启后端，重新问一遍"coke-cola有多少个订单"，确认这次一次调用就能给出准确数字——延续本次会话里已经用过的验证方式（临时加调试日志复现→确认→清理日志）。
