from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from app.graphrag.ontology import Term, resolve_term
from app.graphrag.ontology_categories import TermTypeCategory

if TYPE_CHECKING:
    from app.graphrag.neo4j_client import Neo4jGraphClient

# 与 neo4j_client.py::_RELATION_TYPE_NAME_PATTERN 保持同一份格式约束（有意重复定义，
# 不做跨模块导入——两处校验的是同一条注入防线契约，但分属"解析请求参数"和"拼
# Cypher"两个不同职责层，各自独立演化不构成重复劳动，见 docs/superpowers/specs/
# 2026-08-17-structured-filter-query-tool-design.md 第4节）。
_RELATION_TYPE_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")

# 与 ontology_categories.py::_EXTRA_FIELD_NAME_PATTERN 保持同一份格式约束（有意重复
# 定义，不做跨模块导入——两处校验的是同一条注入防线契约，但分属"声明字段时的格式
# 校验"和"把已确认字段名拼进 Cypher 文本前的防御性复检"两个不同职责层。后者存在
# 的必要性：_migrate_extra_fields_value_shape_if_needed 会把 2026-08-16 之前的历史
# 遗留 extra_fields 直接用 SQL UPDATE 升级成 ExtraFieldSpec，不经过
# _validate_extra_field_specs，所以 spec.name == field 的成员校验本身不能保证字段名
# 格式安全，见 docs/superpowers/specs/2026-08-17-structured-filter-query-tool-design.md
# 第4节）。
_EXTRA_FIELD_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")

_MAX_HOPS = 2
_RESERVED_FIELD_NAME = "standard_name"

_VALID_EXPAND_DIRECTIONS = frozenset({"outgoing", "incoming", "both"})
_VALID_EXPAND_HOPS = frozenset({1, 2})

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


@dataclass(frozen=True)
class GroupBy:
    constraint_index: int


@dataclass(frozen=True)
class StructuredFilterQueryArgs:
    anchor: NameAnchor | TypeAnchor
    constraints: list[AttributeConstraint | RelationConstraint]
    expand: ExpandSpec | None
    group_by: GroupBy | None
    limit: int


def _parse_hop(raw: dict) -> Hop:
    if not isinstance(raw, dict):
        raise StructuredFilterQueryError(f"hop 必须是 dict，收到: {raw!r}")
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
    if not isinstance(raw, dict):
        raise StructuredFilterQueryError(f"constraint 必须是 dict，收到: {raw!r}")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in _VALID_KINDS:
        raise StructuredFilterQueryError(f"constraint.kind 必须是 attribute/relation，收到: {kind!r}")
    if kind == "attribute":
        try:
            field = raw["field"]
            operator = raw["operator"]
            value = raw["value"]
        except KeyError as exc:
            raise StructuredFilterQueryError(f"attribute 约束缺少必填字段: {exc}") from exc
        if not isinstance(operator, str) or operator not in _VALID_OPERATORS:
            raise StructuredFilterQueryError(f"不支持的 operator: {operator!r}")
        return AttributeConstraint(field=field, operator=operator, value=value)
    try:
        raw_hops = raw["hops"]
        target_field = raw["target_field"]
        target_operator = raw["target_operator"]
        target_value = raw["target_value"]
    except KeyError as exc:
        raise StructuredFilterQueryError(f"relation 约束缺少必填字段: {exc}") from exc
    if not isinstance(raw_hops, list):
        raise StructuredFilterQueryError(f"hops 必须是 list，收到: {raw_hops!r}")
    if not raw_hops:
        raise StructuredFilterQueryError("relation 约束的 hops 不能为空")
    if len(raw_hops) > _MAX_HOPS:
        raise StructuredFilterQueryError(f"hops 最多 {_MAX_HOPS} 跳，收到 {len(raw_hops)} 跳")
    if not isinstance(target_operator, str) or target_operator not in _VALID_OPERATORS:
        raise StructuredFilterQueryError(f"不支持的 target_operator: {target_operator!r}")
    hops = [_parse_hop(h) for h in raw_hops]
    return RelationConstraint(
        hops=hops, target_field=target_field, target_operator=target_operator, target_value=target_value,
    )


def _parse_group_by(raw: dict | None, *, constraints: list[AttributeConstraint | RelationConstraint]) -> GroupBy | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StructuredFilterQueryError(f"group_by 必须是 dict，收到: {raw!r}")
    try:
        constraint_index = raw["constraint_index"]
    except KeyError as exc:
        raise StructuredFilterQueryError(f"group_by 缺少必填字段: {exc}") from exc
    if not isinstance(constraint_index, int) or isinstance(constraint_index, bool):
        raise StructuredFilterQueryError(f"group_by.constraint_index 必须是整数，收到: {constraint_index!r}")
    if constraint_index < 0 or constraint_index >= len(constraints):
        raise StructuredFilterQueryError(f"group_by.constraint_index {constraint_index} 越界")
    if not isinstance(constraints[constraint_index], RelationConstraint):
        raise StructuredFilterQueryError("group_by.constraint_index 必须指向一个 relation 约束")
    return GroupBy(constraint_index=constraint_index)


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
    if not isinstance(hops, int) or isinstance(hops, bool) or hops not in _VALID_EXPAND_HOPS:
        raise StructuredFilterQueryError(f"expand.hops 必须是 1 或 2，收到: {hops!r}")
    direction = raw.get("direction", "both")
    if direction not in _VALID_EXPAND_DIRECTIONS:
        raise StructuredFilterQueryError(
            f"expand.direction 必须是 {sorted(_VALID_EXPAND_DIRECTIONS)} 之一，收到: {direction!r}"
        )
    relation_type = raw.get("relation_type")
    return ExpandSpec(hops=hops, relation_type=relation_type, direction=direction)


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
    if expand is not None and group_by is not None:
        raise StructuredFilterQueryError(
            "expand 和 group_by 不能同时使用——group_by 返回的是聚合统计结果，不是具体实体列表，展开邻居没有意义"
        )
    return StructuredFilterQueryArgs(
        anchor=anchor, constraints=constraints, expand=expand, group_by=group_by, limit=limit,
    )


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
        if spec.name == field:
            if not _EXTRA_FIELD_NAME_PATTERN.match(spec.name):
                raise StructuredFilterQueryError(
                    f"字段 {spec.name!r} 未通过命名格式校验（可能是 2026-08-16 之前声明的历史遗留字段，"
                    f"当时还没有这层格式校验），出于安全考虑不能用于结构化查询——"
                    f"需要在管理后台重新声明这个字段才能通过校验"
                )
            return spec.value_type
    available_fields = sorted({_RESERVED_FIELD_NAME} | {spec.name for spec in category.extra_fields})
    raise StructuredFilterQueryError(
        f"字段 {field!r} 不是 {term_type!r} 已确认的属性字段，可用字段: {available_fields}"
    )


def _validate_operator_for_value_type(*, field: str, operator: str, value_type: str) -> None:
    allowed = _OPERATORS_BY_VALUE_TYPE[value_type]
    if operator not in allowed:
        raise StructuredFilterQueryError(
            f"字段 {field!r}（类型 {value_type!r}）不支持运算符 {operator!r}，可用运算符: {sorted(allowed)}"
        )


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
    if not isinstance(resolved.term_type, str) or resolved.term_type not in term_type_schema:
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
        if term is not None and term.term_type == term_type:
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


_CORE_TERM_FIELDS = frozenset({"tenant_id", "node_key", "standard_name", "type"})

# Neo4j 历史 :Term 节点上残留的 product_line 属性不清理（这是本次移除 product_line
# 概念时的既定决定，图谱侧的历史数据不做批量迁移），但也不能以"实体自定义属性"的
# 身份泄露进结构化查询结果、进而出现在 LLM 上下文里——这里显式排除，跟
# _CORE_TERM_FIELDS 分开定义是为了保留语义区分：前者是当前仍然活跃的核心字段，
# 后者是历史遗留、已经不该存在但可能仍物理存在于旧数据上的字段。
_LEGACY_RESIDUAL_NODE_PROPERTIES = frozenset({"product_line"})


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
            return {"matched_count": 0, "anchors": []}
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
        resolved_constraints = _resolve_fuzzy_constraint_values(
            args.constraints, anchor_term_type=resolved.term_type,
            terms=terms, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}
    args = replace(args, constraints=resolved_constraints)

    # 执行阶段单独兜一层 except Exception（比 StructuredFilterQueryError 宽）——这层
    # 边界要防的是"图谱后端挂了/Cypher 运行时报错"（Neo4j 驱动异常、数组谓词碰到
    # 某个节点上不是 list 的属性等），和"入参不合法"是两类失败，但对 Agent 的要求
    # 一样：降级成这一次工具调用的错误观察结果，而不是让异常穿过
    # planner.py::run_tool_calls 的 asyncio.gather(return_exceptions=True) 被重新
    # 抛出、把整个 SSE 回合打挂。
    try:
        result = await graph_client.execute_structured_filter_query(
            args, resolved=resolved, tenant_id=tenant_id, term_type_schema=term_type_schema,
        )
    except Exception as exc:
        return {"error": f"图谱查询执行失败：{exc}"}

    if "groups" in result:
        return result  # group_by 分支：{"groups": [...]}

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
