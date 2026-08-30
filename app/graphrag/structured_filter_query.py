from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from app.graphrag.ontology import Term, resolve_term, resolve_term_or_candidates
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination, to_combination_keys
from app.graphrag.ontology_recall import precision_match_score

if TYPE_CHECKING:
    from app.graphrag.neo4j_client import Neo4jGraphClient

logger = logging.getLogger(__name__)

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
    运算符和字段声明的类型不匹配、hops 超过2跳等。调用方（
    app/agent/tools/structured_filter_query/tool.py::StructuredFilterQueryTool）
    捕获这个异常，转成结构化 {"error": ...} 观察结果返回给 LLM，不让它作为
    未处理异常向上传播——见
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
    # limit=0 是合法的特殊值——表示纯计数意图，不需要具体样本实体，见
    # execute_structured_filter_query()/run_structured_filter_query() 对 0 的
    # 处理（跳过样本查询、不附带 truncated 字段）。只拒绝负数。
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise StructuredFilterQueryError(f"limit 必须是非负整数，收到: {limit!r}")
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


# 精确匹配落空后走模糊兜底的两个门槛。最高分要过 _MIN_SCORE，且要比第二名
# 高出 _MIN_MARGIN——分数接近时宁可报错也不猜：猜错会把整条查询悄悄导向错误
# 实体，用户拿到一个看起来正常、其实答非所问的数字，比直接失败更糟。
_CONSTRAINT_FUZZY_MIN_SCORE = 0.6
_CONSTRAINT_FUZZY_MIN_MARGIN = 0.15

_FANOUT_WARNING_NOTE = (
    "这条路径的第 {hop_index} 跳「{hop}」是多对多关系（单个「{from_term_type}」"
    "最多关联 {fanout} 个「{to_term_type}」），计数经过它中转后会把归属放大："
    "matched_count 不是「{to_term_type}」名下的真实数量，最多可能被放大到 "
    "{fanout} 倍。回答时必须说明这个数字是经中转推导出的关联数、不是精确归属"
    "计数，不要把它当作确定答案给出。"
)


def _best_fuzzy_term_name(value: str, *, term_type: str, terms: list[Term]) -> str | None:
    """在指定 term_type 的候选里，按字符匹配度找最像 value 的那个标准名。

    只在 resolve_term 精确匹配（standard_name/别名字面相等）落空后才调用。
    打分复用 ontology_recall.precision_match_score（"候选名被 value 覆盖了
    多少"），跟召回侧、term_guard 侧共用同一套字符匹配原语。
    """
    best_name: str | None = None
    best = second = 0.0
    for term in terms:
        if term.term_type != term_type:
            continue
        score = max(
            precision_match_score(value, candidate)
            for candidate in (term.standard_name, *term.aliases)
            if candidate
        )
        if score > best:
            best, second, best_name = score, best, term.standard_name
        elif score > second:
            second = score
    if best < _CONSTRAINT_FUZZY_MIN_SCORE or best - second < _CONSTRAINT_FUZZY_MIN_MARGIN:
        return None
    return best_name


def _resolve_or_raise(value: object, *, term_type: str, terms: list[Term]) -> str:
    """把约束值解析成术语表里的标准名：先精确匹配，落空则模糊兜底。

    模糊兜底这一层是 2026-08-28 补的。在那之前这里只有 resolve_term 的精确
    匹配，但工具的 _USAGE_GUIDE 一直向 LLM 承诺"standard_name 的 eq/ne 比较
    值支持别名/模糊匹配，不要求填精确的标准名称"，函数名也叫
    _resolve_fuzzy_constraint_values——承诺和实现对不上。以前没暴露，是因为
    深层参数生成 LLM 通常会自己把用户的口语说法换成标准名；Layer 2 引入
    is_verbatim、要求"完整保留用户原话措辞"之后，它开始把用户的错拼原样带
    进 target_value（实测 "coke-cola" 而不是 "Coca-Cola"），整条查询就以
    "约束值无法解析" 失败了。
    """
    if isinstance(value, str):
        term = resolve_term(value, terms, term_type_hint=term_type)
        if term is not None and term.term_type == term_type:
            return term.standard_name
        fuzzy_name = _best_fuzzy_term_name(value, term_type=term_type, terms=terms)
        if fuzzy_name is not None:
            return fuzzy_name
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


def _correct_hop_directions(
    constraint: RelationConstraint,
    *,
    start_term_type: str,
    allowed_combinations: list[AllowedCombination],
) -> RelationConstraint:
    """把 hops 里方向填反的跳纠正过来。

    direction=outgoing 表示沿 (当前类型 --R--> 目标类型) 正向走，前提是存在
    AllowedCombination(subject=当前, relation=R, object=目标)；incoming 表示
    沿反向边走，前提是存在 (subject=目标, relation=R, object=当前)。

    2026-08-28 实测动机：同一个问题"coke-cola公司有多少个订单"跑 6 次，
    query_intent 完全相同的情况下，5 次生成 outgoing/outgoing（正确，返回
    10000），1 次生成 incoming/incoming（返回 0，Planner 于是如实回答"0 个
    订单"）。差别只在方向，不在意图理解——hop 的 direction 是纯机械的图
    遍历细节，不是 LLM 想表达的语义，而它到底该往哪边走完全可以拿已声明的
    组合确定性地判出来，不用猜。

    只在"给定方向没有任何已声明组合支持、而反方向有"时才翻转：这时候唯一
    合法的解释就是反方向。两个方向都合法时不动（那时方向确实是 LLM 要表达
    的语义）；两个方向都不合法时也不动，让它自然查出空结果，而不是替它编
    一条不存在的路径。allowed_combinations 为空（调用方没提供判据）时整个
    纠正逻辑跳过。
    """
    if not allowed_combinations:
        return constraint
    declared = to_combination_keys(allowed_combinations)
    corrected: list[Hop] = []
    current_term_type = start_term_type
    for hop in constraint.hops:
        forward = (current_term_type, hop.relation_type, hop.target_term_type) in declared
        backward = (hop.target_term_type, hop.relation_type, current_term_type) in declared
        if forward != backward:
            corrected.append(replace(hop, direction="outgoing" if forward else "incoming"))
        else:
            corrected.append(hop)
        current_term_type = hop.target_term_type
    return replace(constraint, hops=corrected)


async def _probe_fanout_warning(
    constraints: list[AttributeConstraint | RelationConstraint],
    *,
    graph_client: "Neo4jGraphClient",
    tenant_id: str,
    anchor_term_type: str,
) -> dict[str, Any] | None:
    """计数场景下检查路径上有没有扇出陷阱，有就返回一份警告，没有返回 None。

    只在路径有 2 跳及以上时才探测，且探测【每一跳，包括第一跳】：单跳路径是
    数据直接断言的事实——没有复合，没有借助中间节点向第三方传递归属这一步，
    天然安全（"Coca-Cola 卖多少种产品"走一跳 产品→公司，那条边本身是多对多，
    但结果是对的，因为压根不存在"传递"）。两跳及以上就不一样了：路径
    A→B→C 只有在每一跳都是（沿探测方向的）函数关系（N:1）时才保得住 A 对 C
    的归属，只要其中任意一跳是多对多，路径就会凭空捏造归属——第一跳同样可以
    是那个多对多的元凶，不能因为它挨着锚点就当成安全的：2026-08-29 实测
    订单号 --CONTAINS--> 产品 --BELONG_TO--> 公司，第一跳本身就是多对多
    （一笔订单包含多件产品），第二跳才是函数关系，结果依然是让每一笔订单都能
    走到它名下每一件产品所属的公司，按公司分组的计数加总数倍于订单总数。

    见 docs/superpowers/specs/2026-08-29-fan-trap-detection-design.md。

    _MAX_HOPS 目前是 2，所以每个约束至多探测两跳；写成循环是为了上限调整时
    无需改这里的逻辑。命中第一个扇出跳就返回，不继续探测。

    探测本身是建议性的旁路检查，不是主查询——探测失败（Neo4j 超时、两次
    往返之间连接被重置、Neptune 5xx 等瞬时故障）不能连累已经拿到的正确
    total_count 一起报废成一次失败的工具调用，所以这里整体兜一层
    except Exception，降级为"没有警告"，只在日志里留痕，跟
    run_structured_filter_query 主查询那层 except Exception 是同一个道理。
    """
    try:
        for constraint in constraints:
            if not isinstance(constraint, RelationConstraint):
                continue
            if len(constraint.hops) < 2:
                continue
            current_term_type = anchor_term_type
            for index, hop in enumerate(constraint.hops):
                fanout = await graph_client.probe_relation_fanout(
                    tenant_id=tenant_id,
                    relation_type=hop.relation_type,
                    from_term_type=current_term_type,
                    to_term_type=hop.target_term_type,
                    direction=hop.direction,
                )
                if fanout > 1:
                    arrow = (
                        f"--{hop.relation_type}-->"
                        if hop.direction == "outgoing"
                        else f"<--{hop.relation_type}--"
                    )
                    hop_label = f"{current_term_type} {arrow} {hop.target_term_type}"
                    return {
                        "hop": hop_label,
                        "fanout": fanout,
                        "note": _FANOUT_WARNING_NOTE.format(
                            hop_index=index + 1,
                            hop=hop_label,
                            from_term_type=current_term_type,
                            to_term_type=hop.target_term_type,
                            fanout=fanout,
                        ),
                    }
                current_term_type = hop.target_term_type
    except Exception:
        logger.warning(
            "_probe_fanout_warning: 扇出探测失败，降级为不发出警告", exc_info=True,
        )
        return None
    return None


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
    allowed_combinations: list[AllowedCombination] | None = None,
) -> dict[str, Any]:
    """structured_filter_query_tool 的执行体调用的编排入口：解析→（NameAnchor 时）
    消歧解析→校验→执行→格式化。"""
    try:
        args = parse_structured_filter_query_args(raw_args)
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}

    if isinstance(args.anchor, NameAnchor):
        candidate = resolve_term_or_candidates(
            args.anchor.name, terms, term_type_hint=args.anchor.type_hint
        )
        if isinstance(candidate, list) and len(candidate) > 1:
            # 同名多候选：绝不从中随便挑一个，也不能压成 matched_count: 0
            # ——那会让 Planner 把"有歧义"说成"没有找到"。返回结构化的候选
            # 清单，让它去问用户是哪一个。
            return {
                "ambiguous_anchor": {
                    "name": args.anchor.name,
                    "candidates": [
                        {
                            "node_key": t.node_key,
                            "standard_name": t.standard_name,
                            "term_type": t.term_type,
                            "extra_properties": t.extra_properties,
                        }
                        for t in candidate
                    ],
                }
            }
        if isinstance(candidate, list):
            # 零命中，语义不变
            return {"matched_count": 0, "anchors": []}
        resolved = ResolvedAnchor(term_type=candidate.term_type, node_key=candidate.node_key)
    else:
        resolved = ResolvedAnchor(term_type=args.anchor.term_type, node_key=None)

    try:
        validate_structured_filter_query(
            args, resolved=resolved,
            confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
    except StructuredFilterQueryError as exc:
        return {"error": str(exc)}

    # 先纠正 hop 方向、再解析约束值：方向纠正只看 term_type 之间的已声明
    # 组合，跟约束值本身无关，两步互不影响，这个顺序只是把"结构性纠错"排在
    # "取值解析"前面，读起来更顺。
    args = replace(args, constraints=[
        _correct_hop_directions(
            c, start_term_type=resolved.term_type, allowed_combinations=allowed_combinations or [],
        )
        if isinstance(c, RelationConstraint) else c
        for c in args.constraints
    ])

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
        # group_by 分支：{"groups": [...]}。它也是聚合语义，同样受扇出影响。
        warning = await _probe_fanout_warning(
            args.constraints, graph_client=graph_client,
            tenant_id=tenant_id, anchor_term_type=resolved.term_type,
        )
        return {**result, "fanout_warning": warning} if warning else result

    rows = result["rows"]
    total_count = result["total_count"]
    if args.limit == 0:
        # 纯计数场景：不能返回 "anchors": [] ——2026-08-28 实测，Planner 会把
        # 空列表读成"没有匹配到任何实体"，进而认定 matched_count 不可信，
        # 明明拿到了精确答案却回复"无法给出确定数字"。这里改成不给 anchors
        # 键、并附一句自描述说明：Planner 看不到工具的 _USAGE_GUIDE（那是
        # 渐进式披露里只给深层参数生成看的），观察结果必须自己解释自己。
        #
        # 但这句自描述只有在路径确实没有扇出陷阱时才成立。命中扇出时换成
        # fanout_warning，且【不能同时保留】原来这句肯定语气的说明——两条
        # 互相矛盾的措辞并存会让模型无所适从。
        warning = await _probe_fanout_warning(
            args.constraints, graph_client=graph_client,
            tenant_id=tenant_id, anchor_term_type=resolved.term_type,
        )
        if warning is not None:
            return {"matched_count": total_count, "fanout_warning": warning}
        return {
            "matched_count": total_count,
            "note": (
                "本次只做计数（limit=0），未返回样本实体。matched_count 是"
                "精确完整的计数，不是上限值、也不是截断值，可以直接作为"
                "确定数字回答用户。"
            ),
        }

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
    # 走到这里 limit 必然 > 0（limit==0 已在上面提前返回），所以"总数比拿到的
    # 样本多"就是真正的截断。见 tool.py 的 _PARAMETERS_SCHEMA.limit 说明。
    if total_count > len(rows):
        payload["truncated"] = True
    return payload
