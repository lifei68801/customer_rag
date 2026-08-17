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


def parse_structured_filter_query_args(raw: dict) -> StructuredFilterQueryArgs:
    """把 LLM 工具调用传来的原始 JSON dict 解析成结构化参数——只做形状校验（必填
    字段是否存在、hops 跳数、operator 是否在协议允许的枚举里），不查 schema 是否
    真的已确认，那是 validate_structured_filter_query 的职责（需要 confirmed_
    relation_types/term_type_schema 这两份数据，本函数没有）。"""
    if not isinstance(raw, dict):
        raise StructuredFilterQueryError(f"结构化过滤查询参数必须是 dict，收到: {raw!r}")
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
    if not isinstance(args.anchor_term_type, str) or args.anchor_term_type not in term_type_schema:
        raise StructuredFilterQueryError(f"anchor_term_type {args.anchor_term_type!r} 不在已确认 schema 里")
    for constraint in args.constraints:
        if isinstance(constraint, AttributeConstraint):
            value_type = _resolve_field_value_type(
                term_type=args.anchor_term_type, field=constraint.field, term_type_schema=term_type_schema,
            )
            _validate_operator_for_value_type(field=constraint.field, operator=constraint.operator, value_type=value_type)
            continue
        for hop in constraint.hops:
            if not isinstance(hop.relation_type, str) or not _RELATION_TYPE_NAME_PATTERN.match(hop.relation_type):
                raise StructuredFilterQueryError(f"关系类型名字不合法: {hop.relation_type!r}")
            if hop.relation_type not in confirmed_relation_types:
                raise StructuredFilterQueryError(f"relation_type {hop.relation_type!r} 不在已确认 schema 里")
            if not isinstance(hop.target_term_type, str) or hop.target_term_type not in term_type_schema:
                raise StructuredFilterQueryError(f"target_term_type {hop.target_term_type!r} 不在已确认 schema 里")
        last_hop = constraint.hops[-1]
        value_type = _resolve_field_value_type(
            term_type=last_hop.target_term_type, field=constraint.target_field, term_type_schema=term_type_schema,
        )
        _validate_operator_for_value_type(
            field=constraint.target_field, operator=constraint.target_operator, value_type=value_type,
        )
