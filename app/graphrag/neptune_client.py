from __future__ import annotations

from typing import Any, Protocol

from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.structured_filter_query import (
    AttributeConstraint,
    ExpandSpec,
    Hop,
    RelationConstraint,
    ResolvedAnchor,
    StructuredFilterQueryArgs,
)

_RESERVED_FIELD_NAME = "standard_name"
_CAST_BY_VALUE_TYPE = {"number": "toFloat", "integer": "toInteger"}

# 独立于 neo4j_client.py 维护的一份查询文本——语义上跟 Neo4j 那边几乎相同
# （Neptune 从 2021 年起原生支持 openCypher），但刻意不 import 共享，见
# 本计划 Global Constraints 的说明。
_SUBGRAPH_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type, 1 AS hops

UNION

MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r:REQUIRES|PRECEDES|PART_OF*2..2]-(related:Term)
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id) AND related <> t
RETURN related.standard_name AS related_name,
       [rel IN r | type(rel)][-1] AS relation_type,
       2 AS hops
"""
# ALL(rel IN r WHERE rel.tenant_id = $tenant_id) must check every edge on the
# 2-hop path, not just one of them — :Term nodes here aren't themselves
# tenant-scoped and can be shared across tenants, so a path that only checks
# one hop could "borrow" a second edge written by a different tenant and leak
# it into this tenant's subgraph. Any future edit to this query that narrows
# the ALL(...) check to a single edge reintroduces a real cross-tenant leak,
# not just a cosmetic bug.
#
# AND related <> t guards against self-loops: Cypher only guarantees a single
# path doesn't reuse the same edge twice, it does not stop a path from going
# out on one edge and coming back on a different one — relation extraction
# routinely produces edges in both directions between the same pair of terms
# (e.g. A-REQUIRES->B and B-PART_OF->A). Without this filter, the 2-hop branch
# would return t itself as if it were "indirectly related to itself".

_ENSURE_INDEXES_QUERIES: list[str] = []
# Neptune 对属性没有 Neo4j 那种显式 CREATE INDEX 语法（它按内部存储结构
# 自动索引全部属性），这里留空——真实生产接入前需要单独调研 Neptune 侧
# 对应的索引/性能调优机制（见 spec 的"未决风险"一节），本计划不假装
# 已经解决这个问题。

_BACKFILL_LEGACY_TERM_NODES_QUERY = """
MATCH (t:Term)
WHERE t.tenant_id IS NULL
SET t.tenant_id = 'default', t.node_key = t.standard_name
"""


class NeptuneClientProtocol(Protocol):
    """AWS Neptune openCypher HTTPS 端点（POST /openCypher，请求体带
    query 文本）的最小封装——单次请求-响应，没有 Neo4j 那种 session/
    transaction 概念。真实实现（认证签名、HTTP 调用、JSON 响应体解析出
    行列表）由接入 Neptune 环境时的具体 client 类完成，这个协议只声明
    NeptuneGraphClient 需要的形状。"""

    async def execute_open_cypher(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


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
    _OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "<>"}
    return f"{prop_expr} {_OPS[operator]} ${param_name}"


def _resolve_cast(
    *, term_type: str, field: str, term_type_schema: dict[str, TermTypeCategory]
) -> str | None:
    if field != _RESERVED_FIELD_NAME:
        return None
    category = term_type_schema.get(term_type)
    if category is None:
        return None
    return _CAST_BY_VALUE_TYPE.get(category.standard_name_value_type)


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


def _build_expand_clause(expand: ExpandSpec) -> str:
    # relation_type 为 None（不限定关系类型）时 rel_pattern 必须是空字符串，不能拼出
    # 一个空的 `:` 类型段——这段模式串是直接字符串插值拼出来的（openCypher 的关系
    # 类型语法本身不能参数化）：relation_type 非空时它已经过 validate_structured_
    # filter_query 的正则格式 + 已确认 relation_type 白名单双重校验，插值才是安全的；
    # 为 None 时干脆不让 `:` 出现在查询文本里，不留任何可以被污染的位置。
    rel_pattern = f":{expand.relation_type}" if expand.relation_type else ""
    if expand.direction == "outgoing":
        arrow_in, arrow_out = "-", "->"
    elif expand.direction == "incoming":
        arrow_in, arrow_out = "<-", "-"
    else:
        arrow_in, arrow_out = "-", "-"
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
# The CASE WHEN neighbor IS NULL THEN NULL ... END wrapper is not decorative:
# an anchor with zero neighbors still produces one row out of OPTIONAL MATCH,
# with `neighbor` bound to null. Without the CASE, collect(DISTINCT {...})
# would build the map literal anyway and collect a single-element list like
# [{related_name: null, ...}] — a fake "neighbor" that only looks real.
# Folding that row to NULL first lets collect() drop it (collect() ignores
# NULL elements), so a neighborless anchor correctly gets neighbors: [].


class NeptuneGraphClient:
    """AWS Neptune 图查询封装，满足 GraphClientProtocol。跟 Neo4jGraphClient
    是两个完全独立的实现——即使查询文本高度相似，也不 import 共享任何
    内部细节，等真的接入 Neptune 环境实测、确认两边查询语义完全一致之后，
    再决定要不要重构出共享部分（YAGNI）。

    真实生产连通性未经验证——见本计划 Global Constraints 和 spec 的
    "未决风险"一节。
    """

    def __init__(self, *, client: NeptuneClientProtocol) -> None:
        self._client = client

    async def query_subgraph(
        self, node_key: str, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        return await self._client.execute_open_cypher(
            _SUBGRAPH_QUERY, {"node_key": node_key, "tenant_id": tenant_id}
        )

    async def execute_structured_filter_query(
        self,
        args: StructuredFilterQueryArgs,
        *,
        resolved: ResolvedAnchor,
        tenant_id: str,
        term_type_schema: dict[str, TermTypeCategory],
    ) -> dict[str, Any]:
        """按已校验的结构化条件筛选 Term 节点并执行 openCypher 查询——调用方
        （app/graphrag/structured_filter_query.py::run_structured_filter_query）
        必须已经跑过 validate_structured_filter_query，本方法不重复校验
        field/relation_type 是否在已确认 schema 里，只负责拼查询文本并通过
        NeptuneClientProtocol 发出去。

        field/target_field/relation_type 都是靠字符串插值直接拼进查询文本的，
        不是参数化传入的——这不是疏漏，是刻意的：openCypher 对动态属性名/关系
        类型的访问只能在运行时按名字解析，插值成静态查询文本之后 Neptune 的
        查询规划器才能在规划阶段命中按 (tenant_id, type, field) 建的索引，
        参数化写法会退化成不带索引的全量扫描。

        插值之所以安全，完全依赖调用方已经替我们做完的两道校验（本方法自己
        不再重复判断）：relation_type 过格式正则（^[A-Z][A-Z0-9_]{0,63}$）+
        该租户已确认 relation_type 成员校验；field/target_field 要么是保留字
        standard_name，要么是该 term_type 已确认 extra_fields 成员，同样经过
        格式校验。任何一处把这条校验链绕过去、直接把未经确认的 LLM 输出拼进
        这里，都会把这个方法变成一个可注入的入口——这条约束是这份代码唯一
        安全的运行前提，不是可选的防御层。

        resolved 由调用方解析 args.anchor 之后传入：resolved.node_key 有值
        （NameAnchor 消歧命中了具体实体）时按 tenant_id + node_key 精确定位
        单个锚点；node_key 为 None（TypeAnchor，按 term_type 扫描候选集合）
        时按 tenant_id + type 定位这一整个 term_type 下的候选集合。
        """
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
            rows = await self._client.execute_open_cypher(query, params)
            return {"groups": rows}

        count_query = f"{anchor_match} WHERE {where_sql} RETURN count(anchor) AS total"
        return_fields = (
            "anchor.standard_name AS standard_name, anchor.node_key AS node_key, "
            "anchor.type AS term_type, properties(anchor) AS all_properties"
        )
        if args.expand is not None:
            # WITH anchor ORDER BY anchor.node_key LIMIT $limit must run before
            # the OPTIONAL MATCH that expands neighbors — $limit bounds the
            # number of anchors, not the number of (anchor, neighbor) row
            # pairs. Moving LIMIT after the expansion would instead truncate
            # the exploded rows: an anchor with many neighbors could eat the
            # whole limit budget by itself, so the same limit=5 would return a
            # shrinking, unpredictable, and wrongly-ordered set of anchors
            # depending on how many neighbors each one happens to have. The
            # ORDER BY has to stay paired with this LIMIT in the same WITH too
            # — anchors must be ranked and cut down before expansion runs, not
            # interleaved with it.
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
        count_rows = await self._client.execute_open_cypher(count_query, params)
        total_count = count_rows[0].get("total", 0) if count_rows else 0
        rows = await self._client.execute_open_cypher(rows_query, rows_params)
        return {"rows": rows, "total_count": total_count}

    async def ensure_tenant_scoped_schema(self) -> None:
        """Neptune 没有 Neo4j 那种显式 CREATE INDEX 语法（见
        _ENSURE_INDEXES_QUERIES 的说明），这里只做存量节点的 tenant_id/
        node_key 回填——跟 Neo4jGraphClient.ensure_tenant_scoped_schema()
        对齐的那一半功能，索引/性能调优这一半是未决风险，留给真实接入
        Neptune 环境时单独调研。"""
        for query in _ENSURE_INDEXES_QUERIES:
            await self._client.execute_open_cypher(query)
        await self._client.execute_open_cypher(_BACKFILL_LEGACY_TERM_NODES_QUERY)
