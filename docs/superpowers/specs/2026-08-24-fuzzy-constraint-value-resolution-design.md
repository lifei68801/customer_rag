# structured_filter_query_tool 的 constraints 支持模糊解析 target_value — 设计文档

## 背景

前序调研（本次会话）确认："coke-cola有多少个订单"这类问题答不出来，根因不是数据问题、不是 schema 问题，是 Planner 的工具调用策略问题：`anchor.name`（模糊消歧，走 `resolve_term()`）和 `constraints` 里的 `target_value`（精确字符串比较，走 Neo4j `=`）是两套完全解耦的匹配机制——LLM 必须先用 `anchor.name` 把"coke-cola"消歧成标准名"Cola"，再用这个标准名发起第二次调用去做 `anchor.term_type=订单号 + constraints` 筛选计数。这不是可以跳过的冗余步骤：如果第一次调用就直接把"coke-cola"塞进 `constraints` 的 `target_value` 做精确匹配，会查出 0 条——因为图谱里存的是"Cola"不是"coke-cola"。

已经验证过的事实：这次会话里发起的真实请求，LLM 在第一次调用里正确完成了消歧（`anchor.name="Cola"`），但**没有接着发起第二次调用**去做真正的计数查询——这是提示词可靠性问题，本次会话里已经在系统提示词里加固过一次（明确 `matched_count` 只在 `anchor.term_type` 模式下权威），但复现测试证明这个加固**没有解决问题**——纯提示词手段对这个场景已经显示出天花板。

本文档设计的方案：让 `constraints` 里针对 `standard_name` 字段的 `eq`/`ne` 比较，也走一遍跟 `anchor.name` 相同的 `resolve_term()` 模糊解析——这样"coke-cola有多少个订单"这类问题，LLM 第一次调用就能直接构造出正确查询（`anchor.term_type=订单号, constraints=[hop到产品, target_value="coke-cola" eq]`），系统内部自动把"coke-cola"解析成"Cola"再比较，**从根上取消"必须两次调用"这个限制**，不再依赖模型"记得"发起第二次调用。

## 已确认的设计决策

1. **适用范围**：只对 `standard_name` 字段（`_RESERVED_FIELD_NAME`）的 `eq`/`ne` 运算符生效。`starts_with` 保持字面前缀匹配（先解析成完整标准名再做前缀匹配，语义不成立）；`extra_fields` 不受影响（通常是编码/数值，模糊匹配没有意义）；数值运算符（`gt`/`gte`/`lt`/`lte`）不受影响。
2. **解析失败的处理**：直接返回结构化错误告诉 LLM"这个值对不上已知实体"，不做静默退回字面比较——让 LLM 当轮就能自我纠正（换个写法，或者先用 `anchor.name` 消歧），而不是让它以为查出的 0 条是真实答案。
3. **两种约束都要覆盖**：`AttributeConstraint`（比较锚点自己的 `standard_name`）用锚点已解析出的 `resolved.term_type` 做类型提示；`RelationConstraint`（比较关系另一端的 `standard_name`）用约束自己声明的 `hops[-1].target_term_type` 做类型提示——两种情况都有明确、非歧义的类型上下文（不像 `anchor.name` 消歧那样可能真歧义），解析不到基本就意味着这个值真的不存在。

## 自查补充的一个边界情况

`standard_name` 字段本身可以被声明成数值类型（今天早些时候修的 bug 场景：像"销量"这种"每个取值都是独立节点"的 term type，`standard_name_value_type="number"`）——对这类 term type 的 `standard_name` 做 `eq` 比较，比较的是真实数字（比如"销量=100"），**不能**尝试模糊解析成实体名。所以新增的解析逻辑必须先检查相关 term_type 的 `standard_name_value_type` 是否为 `"string"`，只有这种情况才走模糊解析；否则维持现状（字面值直接参与比较，跟 `_resolve_cast` 已有的数值转换逻辑走原来的路，本次改动完全不碰）。

## 架构：一个纯函数式的解析 pass，插在校验之后、执行之前

```
parse_structured_filter_query_args（不变）
  → resolve anchor（NameAnchor→ResolvedAnchor，不变）
  → validate_structured_filter_query（不变——只查字段/运算符/关系类型合法性，不关心具体取值内容，
     所以不需要等值解析完再校验，顺序上校验在前更自然）
  →【新增】_resolve_fuzzy_constraint_values：只有校验通过、确认查询"形状"合法之后，
     才对 constraints 里 standard_name 的 eq/ne 比较值尝试模糊解析
  → execute_structured_filter_query（不变——拿到的已经是解析好的标准名，
     Cypher 生成逻辑、neo4j_client.py 完全不用碰）
```

这个改动的关键优势：**完全不碰 `neo4j_client.py`、不碰 Cypher 生成、不碰五层白名单校验本身**——纯粹是"校验完形状、执行之前"插入一步"把值本身也翻译成标准名"，复用 `anchor.name` 已经在用的同一个 `resolve_term()`，只是作用对象从"锚点自己的名字"扩展到"约束条件里引用的名字"。

## 详细设计：`app/graphrag/structured_filter_query.py`

顶部导入区加 `replace`（用于不可变数据类的字段替换，构造修改后的新约束对象，不改动原对象——跟这个文件一贯的不可变风格一致）：

```python
from dataclasses import dataclass, replace
```

新增解析函数（放在 `validate_structured_filter_query` 之后、`run_structured_filter_query` 之前）：

```python
_FUZZY_RESOLVABLE_OPERATORS = frozenset({"eq", "ne"})


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
    发起第二次调用"这个两步流程能在一次调用里完成。

    只处理 standard_name + eq/ne 这个组合：starts_with 是字面前缀匹配，先解析
    成标准名再做前缀匹配语义上不成立；extra_fields 通常是编码/数值，模糊匹配
    没有意义；数值比较运算符本来就不适用字符串解析。此外还要求该字段这次
    确实被声明成字符串类型（value_type == "string"）——standard_name 本身也
    可能被声明成数值类型（比如"销量"这种"值即节点名"的 term type），那种
    情况下 eq 比较的是真实数字，不能被这里拦下来当实体名解析（详见
    ontology_categories.py::TermTypeCategory.standard_name_value_type）。

    解析失败直接抛错，不做静默退回字面比较——宁可让 LLM 当轮看到明确的
    "这个名字对不上已知实体"，也不要生成一条注定查不到东西的查询。
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


def _should_fuzzy_resolve(
    *, field: str, operator: str, term_type: str, term_type_schema: dict[str, TermTypeCategory]
) -> bool:
    if field != _RESERVED_FIELD_NAME or operator not in _FUZZY_RESOLVABLE_OPERATORS:
        return False
    category = term_type_schema.get(term_type)
    # term_type 已经过 validate_structured_filter_query 校验，这里 category 必然存在；
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

`run_structured_filter_query` 里，在 `validate_structured_filter_query` 成功之后、`execute_structured_filter_query` 之前插入调用：

```python
    try:
        validate_structured_filter_query(
            args, resolved=resolved,
            confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}

    try:
        resolved_constraints = _resolve_fuzzy_constraint_values(
            args.constraints, anchor_term_type=resolved.term_type,
            terms=terms, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}
    args = replace(args, constraints=resolved_constraints)

    try:
        result = await graph_client.execute_structured_filter_query(
            args, resolved=resolved, tenant_id=tenant_id, term_type_schema=term_type_schema,
        )
```

（后续 `execute_structured_filter_query` 调用不用改——它拿到的 `args` 局部变量已经是替换过 `constraints` 的新对象，`args.anchor`/`args.expand`/`args.group_by`/`args.limit` 都原样保留。）

## 工具 schema / 系统提示词要不要跟着改

`app/agent/tools.py::STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 里 `constraints` 字段的描述文案值得顺手更新一句，明确告诉 LLM 现在可以直接把口语化的名字填进 `target_value`：在现有 `constraints` 的 description 末尾补一句"`standard_name` 字段的 `eq`/`ne` 比较值支持别名/模糊匹配，不要求填精确的标准名称"。`_PLANNER_SYSTEM_PROMPT` 不强制改（这条能力是工具 schema 自己的说明就能覆盖的细节，不需要在系统级提示词里重复），但如果这次要一并处理"先消歧再计数通常需要两次调用"这句话现在是否还准确——可以顺手弱化措辞（从"通常需要两次调用"改成"多数情况一次调用就够，只有 anchor.name 消歧本身有歧义时才需要先消歧"），避免继续暗示两次调用是常态。

## 测试

- `_should_fuzzy_resolve`：`standard_name`+`eq`/`ne`+`value_type=string` 时返回 `True`；`standard_name`+`eq`+`value_type=number`（销量场景）时返回 `False`；非 `standard_name` 字段/`starts_with`/未知 term_type 时返回 `False`。
- `_resolve_or_raise`：能解析时返回标准名；解析不到、或传入非字符串值时都抛 `StructuredFilterQueryError`，错误信息包含被拒绝的原始值和目标 term_type。
- `_maybe_resolve_attribute_constraint`/`_maybe_resolve_relation_constraint`：分别覆盖"命中并替换"“不满足条件原样透传”“解析失败抛错"三种路径。
- `run_structured_filter_query` 端到端（复用现有 `_FakeGraphClient` 模式）：
  - `anchor.term_type` + `RelationConstraint` 的 `target_value` 用口语化别名（如"coke-cola"），断言最终传给 `graph_client.execute_structured_filter_query` 的 `args.constraints[0].target_value` 已经是解析后的标准名"Cola"，不是原始输入。
  - 同一个别名场景，`terms` 里不存在能匹配的术语时，返回 `{"error": ...}`，且 `graph_client.execute_structured_filter_query` 从未被调用（用会在被调用时 `raise AssertionError` 的假客户端断言）。
  - `AttributeConstraint`（锚点自己的 `standard_name`）用别名时同样能解析——覆盖跟 `RelationConstraint` 不同的类型提示来源（`resolved.term_type` vs `hops[-1].target_term_type`）。
  - "销量"这类 `standard_name_value_type="number"` 的 term type，`eq` 比较传数字时完全不触发模糊解析、行为跟这次改动之前完全一致（防回归）。
  - `NameAnchor` 模式下 `constraints` 里也一样生效（不只是 `TypeAnchor` 模式）——虽然实际业务场景较少见（已知具体实体又要按名字过滤），但代码路径上不应该有特殊排除。
- （可选，人工验证）用真实环境重跑"coke-cola有多少个订单"，确认这次一次调用就能给出准确数字，不再需要"先消歧再计数"两轮。

## 影响范围

- `app/graphrag/structured_filter_query.py`：新增 4 个私有函数 + `run_structured_filter_query` 里插入 5 行调用代码，`replace` 一处新增导入。
- `app/agent/tools.py`：`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 的 `constraints` 描述文案补一句（可选，不改变行为，只是让 LLM 更容易发现这个能力）。
- `app/agent/graph.py`：`_PLANNER_SYSTEM_PROMPT` 里"通常需要两次调用"这句话可以弱化措辞（可选）。
- 不改：`app/graphrag/neo4j_client.py`（Cypher 生成层）、`validate_structured_filter_query`（五层白名单校验本身）、`app/agent/planner.py`（调度层）。
