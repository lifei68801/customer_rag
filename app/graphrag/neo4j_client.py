from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.graphrag.ontology import Term
from app.graphrag import provenance
from app.graphrag.structured_filter_query import (
    AttributeConstraint,
    ExpandSpec,
    Hop,
    RelationConstraint,
    ResolvedAnchor,
    StructuredFilterQueryArgs,
)

from app.graphrag.ontology_categories import TermTypeCategory

if TYPE_CHECKING:
    from app.graphrag.ontology_categories import ExtraFieldSpec

_RELATION_TYPE_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")

# 跟 structured_filter_query.py::_RESERVED_FIELD_NAME 保持同一份约定，独立定义
# 不做跨模块导入——原因同文件顶部 _RELATION_TYPE_NAME_PATTERN 的说明。
_RESERVED_FIELD_NAME = "standard_name"
_CAST_BY_VALUE_TYPE = {"number": "toFloat", "integer": "toInteger"}

# 详情页专用：一跳邻居，带 node_key 和方向。
#
# 不复用 _SUBGRAPH_QUERY——那个是给 agent 检索用的，只返回 standard_name，
# 详情页要能点击跳到邻居，光有显示名跳不了；而且它把 2 跳链式关系也算进
# 来，详情页只关心直接相连的。
#
# 方向要分出来：「公司 生产 产品」和「产品 生产 公司」是两回事，混在一起
# 看不出这个实体在关系里扮演什么角色。
_TERM_RELATIONS_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term)
WHERE r.tenant_id = $tenant_id
RETURN CASE WHEN startNode(r) = t THEN 'out' ELSE 'in' END AS direction,
       type(r) AS relation_type,
       related.node_key AS node_key,
       related.standard_name AS standard_name,
       related.type AS term_type
ORDER BY relation_type, standard_name
"""

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
# 第二段 UNION 只对 REQUIRES/PRECEDES/PART_OF 这三种"链式"关系放开到
# 恰好 2 跳（*2..2，不是 *1..2，避免和第一段的 1 跳结果重复）——前提链、
# 流程顺序、包含层级经常需要连续追问两步；其余关系类型语义上查 1 跳就
# 有意义，继续放开多跳容易发散、引入噪声上下文。
#
# ALL(rel IN r WHERE rel.tenant_id = $tenant_id) 必须校验路径上每一条边
# 的租户归属，不能只查其中一条——:Term 标准节点本身不分租户、可能被
# 多个租户共用，如果只检查一跳，2 跳路径有可能"借道"另一个租户写入的边，
# 把不该出现的信息泄露给当前租户。这是本次改动里唯一一个如果实现疏忽
# 会导致真实安全问题的点。
#
# AND related <> t 是自环守卫：Cypher 的关系唯一性规则只保证一条路径内
# 不重复使用同一条边，并不能阻止"去程用一条边、回程用另一条边"绕回起点
# ——关系抽取经常在同一对术语之间产出双向边（如 A-REQUIRES->B 又
# B-PART_OF->A），若不加这个过滤，2 跳查询会把 t 自己当成"与自己间接
# 关联"的结果返回。

# 保留关系类型：ALIAS_OF 只能由 sync_term 写入别名边（不带 tenant_id/source/
# provenance，语义和 merge_relation 写入的关系边不同）——merge_relation 硬性
# 拒绝它，避免同一个关系类型下混入两种不兼容语义的边。
_RESERVED_RELATION_TYPES = frozenset({"ALIAS_OF"})

_COMPARISON_OPERATOR_TO_CYPHER = {
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "<>",
}


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
    # relation_type 为 None（任意关系类型）时 rel_pattern 必须是空字符串，不能拼出
    # 一个空的 `:` 类型段——这段 Cypher 模式串直接由字符串插值拼出（Neo4j 的关系
    # 类型语法本身不能参数化），relation_type 非空时它已经过 validate_structured_
    # filter_query 的正则格式 + 已确认 relation_type 白名单双重校验（Task 6），插值
    # 才是安全的；为 None 时干脆不让 `:` 出现在查询文本里，不留任何可以被污染的
    # 位置。
    rel_pattern = f":{expand.relation_type}" if expand.relation_type else ""
    # 关系模式两端都必须有一个 "-"（Cypher 语法本身要求），箭头只是在其中一端
    # 额外加的方向标记——outgoing 是右端箭头 "->"，incoming 是左端箭头 "<-"，
    # both 两端都不带箭头但两端的 "-" 依然要有，写成 arrow_in="" 会拼出
    # "(anchor)[r...]" 这种缺一半基础语法的非法 Cypher 模式串。
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
# CASE WHEN neighbor IS NULL THEN NULL ELSE {...} END 里的 NULL 分支是关键：
# OPTIONAL MATCH 在锚点没有邻居时仍然产出一行、neighbor 绑定为 null，如果直接
# collect(DISTINCT {related_name: neighbor.standard_name, ...}) 会把这一行也
# 收进列表变成 [{related_name: null, ...}]——CASE 判空后整行折叠成 NULL，
# collect() 会自动丢弃 NULL 元素，让"没有邻居"的锚点拿到 neighbors: []
# 而不是 [{related_name: null, ...}] 这种看起来像邻居实际是噪声的假邻居。

# 关系边有向（MERGE (a)-[:TYPE]->(b)），按有向模式匹配删除保证每条边只
# 命中一次；r.source 只有 merge_relation 写入的抽取关系才有，sync_term/
# sync_terms 写入的 ALIAS_OF 边没有这个属性，天然不会被误删。
# ALIAS_OF 边（sync_term 写入）从不设置 tenant_id，这条按 r.tenant_id 精确
# 匹配的过滤天然把它们排除在外（Cypher 里 null = $tenant_id 恒为假）——
# 不需要额外按关系类型区分"这条边要不要按租户过滤"。
_DELETE_RELATIONS_BY_SOURCE_QUERY = """
MATCH ()-[r]->() WHERE r.source = $source AND r.tenant_id = $tenant_id
DELETE r
"""

_COUNT_STALE_RELATIONS_QUERY = """
MATCH (a:Term {tenant_id: $tenant_id})-[r]->(b:Term {tenant_id: $tenant_id})
WHERE r.tenant_id = $tenant_id
  AND r.source = $source
  AND r.provenance = $provenance
RETURN
  count(r) AS total,
  count(CASE WHEN r.recorded_at < $before THEN 1 END) AS stale
"""
# 关系侧安全阀的计数：同一次查询里数出该源文件下 ETL 写过的边总数，以及
# 其中"本轮没有重写过"（recorded_at 严格早于本轮时间戳）的条数。后者就是
# delete_stale_relations_by_source 将要删掉的那批，条件逐字一致。
#
# 分母用的是**写入之后**的总数（本轮重写的 + 陈旧的）。源文件如果被截断，
# 本轮重写得少、陈旧的多，比值就高——这正是要挡住的事故形态。


_DELETE_STALE_RELATIONS_QUERY = """
MATCH (a:Term {tenant_id: $tenant_id})-[r]->(b:Term {tenant_id: $tenant_id})
WHERE r.tenant_id = $tenant_id
  AND r.source = $source
  AND r.provenance = $provenance
  AND r.recorded_at < $before
DELETE r
RETURN count(r) AS removed
"""
# 只删本次运行没有重写过的边：merge_relation 每次 MERGE 都无条件
# SET r.recorded_at，所以本轮写过的边时间戳恰好等于本轮的值，严格早于
# 它的就是"上一轮写过、这一轮源里已经没有"的陈旧边。
#
# recorded_at 存的是 "%Y-%m-%d %H:%M:%S" 字符串，这个格式的字典序等于
# 时序，可以直接用 < 比较。
#
# 已知边界：两轮 ETL 在同一秒内跑完时，上一轮的时间戳与本轮相同、匹配
# 不到，陈旧边会残留到下一轮。实际不可能——单轮 ETL 远超一秒，且
# etl_runs 上有"每租户同时只能有一个 running"的唯一索引。这个边界不去
# 消除它，留着靠下一轮 ETL 自愈。
#
# provenance 也进过滤条件：同名 source 的边如果是抽取管道写的
# （AUTO_MERGED / HUMAN_APPROVED），不该被 ETL 的清理波及。
#
# RETURN count(r) AS removed 的返回值语义已经用真实 Neo4j 5.22（本项目
# docker-compose.yml 里固定的版本）验证过：DELETE 之后 r 仍然绑定着被删除
# 的那些关系记录，count(r) 统计的是这次匹配+删除的行数，不是删除后图里
# 剩余的边数——三条边中两条命中过滤条件时，脚本验证 RETURN 值为 2，
# 删除后图里确实只剩 1 条边，两者互相印证。

# 别名节点用 alias_name 属性而不是 standard_name——避免和 _SUBGRAPH_QUERY
# 按 tenant_id/node_key 精确匹配标准节点的查询模式产生歧义（别名节点本身
# 不该被当成标准节点查到）。
_SYNC_TERM_QUERY = """
MERGE (t:Term {tenant_id: $tenant_id, node_key: $node_key})
SET t.standard_name = $standard_name, t.type = $type
SET t += $extra_properties
WITH t
UNWIND $aliases AS alias_name
MERGE (a:Term {alias_name: alias_name})
MERGE (a)-[:ALIAS_OF]->(t)
"""

_COUNT_TERM_RELATION_EDGES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-()
WHERE r.tenant_id = $tenant_id AND type(r) <> 'ALIAS_OF'
RETURN count(DISTINCT r) AS edge_count
"""
# WHERE r.tenant_id = $tenant_id 是租户隔离的一部分，不只是性能过滤：只按
# 两端节点的 tenant_id 匹配的话，别的租户写的边会挡住本租户的术语删除
# （真实库里就有一条两端节点 tenant_id=default、边自己 tenant_id=demo 的
# 历史脏边）。拿 r.tenant_id 做判据是有依据的：merge_relation 写边时两端节点
# 的 MERGE 匹配属性和边上的 tenant_id 用的是同一个 $tenant_id 参数，“边的
# 租户”按设计恒等于“两端节点的租户”；这条过滤只会排掉违反这个不变式
# 的脏数据，不会误伤合法的边。跟 _TERM_RELATIONS_QUERY / _SUBGRAPH_QUERY 已经在
# 用的过滤口径一致，守卫看到的边和详情页列出的边因此是同一批。
#
# count(DISTINCT r) 而不是 count(r)：无向模式 (t)-[r]-() 在自环（一个节点
# 指向自己）上会从两个方向各匹配一次、产出两行，但绑定的是同一条边，
# count(r) 会把 1 条边报成 2 条。去重按边身份而不是按行，无论从哪个方向
# 匹配到都只算一次；保留无向模式是必要的——守卫关心的是“这个术语参与了
# 任何关系边”，入边和出边都算。
#
# ALIAS_OF 是术语表→图谱的结构性同步边（sync_term 写入，见上面
# _SYNC_TERM_QUERY），不代表"这个术语已经出现在真实知识图谱数据里"；
# 删除前的守卫检查只关心 LLM 抽取/人工审核产出的关系边（merge_relation
# 写入的 RELATED_TO/PART_OF/... 这些），排除 ALIAS_OF 避免每个术语只要
# 有别名就永远无法删除。

_RENAME_TERM_NODE_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})
SET t.standard_name = $new_standard_name
"""
# node_key 不参与这条语句——ADR-0003 的核心断言：改名只更新展示属性
# standard_name，身份键 node_key 创建后永不改变。必须是对同一个节点
# 对象做属性 SET，不能先 DELETE 再 CREATE——Neo4j 的关系边挂在节点对象
# 上，不是按属性值查找的，原地改属性不会影响节点已有的任何关系边。

_DELETE_TERM_NODE_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})
OPTIONAL MATCH (a:Term)-[:ALIAS_OF]->(t)
DETACH DELETE t, a
"""
# 连同别名节点一起删——sync_term() 建的别名节点除了指向这个标准术语
# 没有其它用途，标准术语被删后别名节点留着就是纯垃圾数据。OPTIONAL
# MATCH 让"没有别名"的术语也能正常匹配到 t（DELETE 一个 null 值是
# Cypher 里的合法操作，不会报错）。

_FANOUT_QUERY_TEMPLATE = """
MATCH (a:Term){arrow_left}[r:{relation_type}]{arrow_right}(b:Term)
WHERE a.tenant_id = $tenant_id AND a.type = $from_term_type
  AND b.tenant_id = $tenant_id AND b.type = $to_term_type
  AND r.tenant_id = $tenant_id
WITH a, count(DISTINCT b) AS k
RETURN max(k) AS fanout
"""
# 扇出探测：单个 from 节点最多能走到几个不同的 to 节点。fanout > 1 说明这一跳
# 不是函数关系，沿它做计数聚合会把归属放大——见 docs/superpowers/specs/
# 2026-08-29-fan-trap-detection-design.md。
#
# relation_type 走 str.format 插值（Cypher 无法参数化关系类型），安全性依赖
# 调用方已跑过 validate_structured_filter_query 的格式正则 + 已确认成员校验，
# 跟 execute_structured_filter_query 的插值理由完全一致。term_type 是普通
# 属性值，一律参数化，并且走 (tenant_id, type) 复合索引
# term_tenant_term_type_idx（见 _ENSURE_INDEXES_QUERIES）。

_ENSURE_INDEXES_QUERIES = [
    "CREATE INDEX term_tenant_node_key_idx IF NOT EXISTS FOR (t:Term) ON (t.tenant_id, t.node_key)",
    "CREATE INDEX term_tenant_term_type_idx IF NOT EXISTS FOR (t:Term) ON (t.tenant_id, t.type)",
]
# 所有节点共享同一个 :Term 标签（"多类型实体"是靠 term_type 取值模拟的，
# 不是原生多标签设计，见 docs/superpowers/specs/2026-08-15-etl-driven-
# schema-construction-design.md §3.4），按 tenant_id/node_key/term_type
# 过滤没有索引可用，量级大的租户（如 MUJI 的 SKU 18万+ 行）没有索引会
# 退化成全表扫描。

_BACKFILL_LEGACY_TERM_NODES_QUERY = """
MATCH (t:Term)
WHERE t.tenant_id IS NULL
SET t.tenant_id = 'default', t.node_key = t.standard_name
"""
# 一次性回填 2026-08-15 之前写入的、没有 tenant_id/node_key 属性的存量
# :Term 节点——WHERE t.tenant_id IS NULL 保证幂等，重复调用只会处理还没
# 打过标记的节点。别名节点（alias_name 属性）不参与这次回填：sync_term
# 的别名节点从来不设置 tenant_id/node_key/standard_name，这次改造不改变
# 别名节点的结构。


class Neo4jSessionProtocol(Protocol):
    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> Any: ...

    async def __aenter__(self) -> "Neo4jSessionProtocol": ...

    async def __aexit__(self, *args: object) -> None: ...


class Neo4jDriverProtocol(Protocol):
    def session(self) -> Neo4jSessionProtocol: ...


class GraphWriteProtocol(Protocol):
    """后台管理路由（本体管理/术语管理/结构化 ETL）需要的写方法集合。

    只覆盖这三个消费方（admin_ontology_routes.py/admin_terms_routes.py/
    schema_etl.py）实际调用的方法——摄取管道用 GraphWriteClientProtocol
    （见 normalization.py），审核队列用 ReviewGraphClientProtocol（见
    review_queue.py），两者是不同的消费场景，故意不并进这里。`Neo4jGraphClient`
    是目前唯一完整实现这个协议的类；`NeptuneGraphClient` 尚未实现，调用会
    命中显式的 NotImplementedError 存根（见 neptune_client.py）——收窄这个
    协议本身不会在 CI 里拦住这类调用（项目 CI 目前只跑 pytest，不跑类型
    检查），存根才是运行时真正生效的防线。
    """

    async def sync_term(self, term: Term) -> None: ...

    async def rename_term_node(
        self, *, tenant_id: str, node_key: str, new_standard_name: str
    ) -> None: ...

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None: ...

    async def probe_relation_fanout(
        self, *, tenant_id: str, relation_type: str, from_term_type: str,
        to_term_type: str, direction: str,
    ) -> int: ...

    async def count_relation_edges_for_term(
        self, *, tenant_id: str, node_key: str
    ) -> int: ...

    async def list_term_relations(
        self, *, tenant_id: str, node_key: str
    ) -> list[dict[str, Any]]: ...

    async def ensure_extra_field_indexes(
        self, *, tenant_id: str, term_type: str, extra_fields: list["ExtraFieldSpec"]
    ) -> None: ...

    async def migrate_relation_type_edges(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int: ...

    async def migrate_term_type_nodes(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int: ...


class Neo4jGraphClient:
    """Neo4j 图查询封装：给定标准术语名，返回与之相关的子图。

    别名到标准名的归一化在应用层（term_matcher）完成，这里只处理
    已归一化的标准名查询，保证返回给 LLM 的上下文使用统一的标准
    名称，而不是原样带入各种不同表述。
    """

    def __init__(self, *, driver: Neo4jDriverProtocol) -> None:
        self._driver = driver

    async def list_term_relations(
        self, *, tenant_id: str, node_key: str
    ) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(
                _TERM_RELATIONS_QUERY,
                {"node_key": node_key, "tenant_id": tenant_id},
            )
            return await result.data()

    async def query_subgraph(
        self, node_key: str, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(
                _SUBGRAPH_QUERY,
                {"node_key": node_key, "tenant_id": tenant_id},
            )
            return await result.data()

    async def execute_structured_filter_query(
        self,
        args: StructuredFilterQueryArgs,
        *,
        resolved: ResolvedAnchor,
        tenant_id: str,
        term_type_schema: dict[str, TermTypeCategory],
    ) -> dict[str, Any]:
        """按已校验的结构化条件筛选 Term 节点——调用方（app/graphrag/
        structured_filter_query.py::run_structured_filter_query）必须已经跑过
        validate_structured_filter_query，本方法不重复校验 field/relation_type
        是否在已确认 schema 里，只负责构造 Cypher 并执行。

        属性字段名（field/target_field）和 relation_type 都走字符串插值拼进查询
        文本，不做参数化——这是刻意的（且是本方法唯一安全的做法）：Neo4j 对动态
        属性访问 t[$param] 只在运行时解析属性名，查询规划器没法在规划阶段用上
        (tenant_id, type, field) 复合索引（ensure_extra_field_indexes 建的那些），
        每次属性过滤都会退化成全表按 type 扫描——这正是 Task 2 的索引本该避免的
        18万+行全表扫描。改成静态插值后规划器才能命中索引。

        安全性依赖调用方已经过 validate_structured_filter_query 的双重校验：
        relation_type 过格式校验（^[A-Z][A-Z0-9_]{0,63}$）+ 已确认 tenant_relation_
        types 成员校验；field/target_field 过"是保留字 standard_name，或是该
        term_type 已确认 extra_fields 的成员"校验。extra_fields 的字段名正常
        情况下在声明时（ontology_categories.py::_validate_extra_field_specs）
        已经过 ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$ 格式校验，但历史遗留数据（2026-
        08-16 之前经 _migrate_extra_fields_value_shape_if_needed 迁移写入的
        字段名）绕过了这层声明时校验——structured_filter_query.py::
        _resolve_field_value_type 因此额外对命中的字段名做了同一份格式校验的
        运行时兜底，两条来源最终都收敛到"格式安全的字面量 + 租户已确认成员
        资格"，插值才是安全的。见
        docs/superpowers/specs/2026-08-17-structured-filter-query-tool-design.md
        第5节。

        resolved 由调用方（run_structured_filter_query）解析 args.anchor 之后
        传入，本方法按 resolved.node_key 是否为空二选一决定锚点怎么定位，不再
        自己判断 args.anchor 是哪种模式：node_key 有值时（NameAnchor 消歧命中
        一个具体实体）按 tenant_id + node_key 精确定位单个锚点；node_key 为
        None 时（TypeAnchor，一开始就要在某个 term_type 下扫描满足条件的一批
        实体）按 tenant_id + type 定位这一整个 term_type 下的候选集合。
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
                continue  # group_by 指向的约束走独立的 MATCH（下方分支），不进 EXISTS
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
            # WITH anchor ORDER BY anchor.node_key LIMIT $limit 必须出现在
            # OPTIONAL MATCH 展开邻居之前——LIMIT 约束的是锚点数量，不是展开后
            # 的 (锚点, 邻居) 行对数量。如果反过来把 LIMIT 放在 OPTIONAL MATCH
            # 之后的 RETURN 上，一个有很多邻居的锚点会在展开阶段先炸出一大批
            # 行，LIMIT 截断的就是这些行而不是锚点本身——同样 limit=5，返回的
            # 锚点数会随每个锚点的邻居数量变化而不可预测地变少，且顺序错误
            # （ORDER BY 也必须在这个 WITH 里跟 LIMIT 配对，锚点排序完成后再展开，
            # 不能让展开插在排序和截断中间）。
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
            count_rows = await count_result.data()
            # 用 .get("total", 0) 而不是直接 ["total"] 做防御性读取——真实 Neo4j 的
            # count() 恒返回恰好一行，这里的防御是为了兼容测试替身 FakeSession（见
            # tests/graphrag/test_neo4j_client.py）用同一份 rows 应答任意次数 .run()
            # 调用的既有用法：一些既有测试的 rows= 构造的是"取行查询"该返回的行形状
            # （不带 total 键），会被同一个 FakeSession 原样喂给计数查询这次调用。
            total_count = count_rows[0].get("total", 0) if count_rows else 0
            # limit=0 是调用方明确表示"只要计数、不要样本"（见 tool.py 的
            # _PARAMETERS_SCHEMA.limit 说明）——rows_query 无论如何都只会
            # 产出 0 行，跳过这次查询本身，省一次不必要的 Neo4j 往返。
            if args.limit == 0:
                rows: list[dict[str, Any]] = []
            else:
                rows_result = await session.run(rows_query, rows_params)
                rows = await rows_result.data()
        return {"rows": rows, "total_count": total_count}

    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
        provenance: str,
        recorded_at: datetime,
    ) -> None:
        """幂等写入一条术语间关系（MERGE，不存在则创建，存在则不重复）。

        source 记录这条边是从哪个文档抽取出来的，写在边的属性上——
        重新摄取同一文档前先按 source+tenant_id 删掉它写过的旧边（见
        delete_relations_by_source），避免文档内容变更后旧关系永久
        残留在图谱里，和 vector_store.delete_by_source() 是同一个思路。

        两端节点的 MERGE 匹配条件现在带 tenant_id——:Term 节点本次改造
        前不分租户、可能被多个租户共用，这是 docs/EXECUTION_PLAN.md 第9节
        列为"尚未做的"多租户隔离项之一，本次一并补齐：不这样做的话两个
        租户各自抽取出同一对术语间的关系时，会共用同一对 Neo4j 节点，
        产生跨租户数据污染。

        subject_standard_name/object_standard_name 这两个参数名是历史
        遗留（不改动，避免连锁改动调用方签名），但它们的值必须是术语的
        node_key（创建时固定的身份键，改名后不变——ADR-0003），不是当前
        的展示名 standard_name：两者只在术语刚创建、尚未被改名时恰好
        相等，改名之后就会不同。调用方（app/graphrag/normalization.py、
        review_queue.py、app/agent/tools/structured_filter_query/tool.py::
        StructuredFilterQueryTool）必须先
        用 resolve_to_standard_name() 等方式解析出 standard_name，再从
        已加载的 terms 列表里按 standard_name 反查对应的 node_key，把
        node_key 传进来——绝不能假定"展示名等于 node_key"，否则改名后
        这里会用旧的 node_key 形状字符串新建一个没有 standard_name
        属性的幽灵节点，而不是命中真实节点。

        tenant_id 必须写进 MERGE 的匹配模式本身（不能只在匹配到之后才
        SET）——:Term 标准节点不分租户、可能被多个租户共用，如果匹配
        条件只看 (a, 关系类型, b) 不看 tenant_id，两个租户各自抽取出同一对
        标准术语间的同类型关系时，第二次 merge_relation 会命中并覆盖第一
        个租户写的那条边（同一条边的 tenant_id/source/provenance 被悄悄
        改写成后来者的），而不是各自新建一条边——这是 2026-08-12 修的
        真实跨租户数据覆盖问题，不是假设性风险。

        provenance 标记这条边是怎么进来的（app/graphrag/provenance.py 的
        AUTO_MERGED："摄取时术语表精确对齐后自动写入"，或
        HUMAN_APPROVED："未对齐候选经人工审核批准后写入"，或
        ETL："结构化 ETL 写入路径产生"）；recorded_at
        是这次写入发生的时间。两者都只是可观测性字段，不参与
        query_subgraph 的检索过滤——检索侧目前仍然不区分来源，一视同仁
        地返回，这是刻意保留的现状（见该模块的说明），加这两个字段只是
        让"这条边有没有被人看过"这件事变得可事后追查。
        """
        if not _RELATION_TYPE_NAME_PATTERN.match(relation_type):
            raise ValueError(
                f"关系类型名字不合法: {relation_type!r}，必须满足 ^[A-Z][A-Z0-9_]{{0,63}}$"
            )
        if relation_type in _RESERVED_RELATION_TYPES:
            raise ValueError(
                f"{relation_type!r} 是保留关系类型，只能由 sync_term 写入别名边，"
                f"不能通过 merge_relation 写入"
            )
        query = (
            "MERGE (a:Term {tenant_id: $tenant_id, node_key: $subject_name}) "
            "MERGE (b:Term {tenant_id: $tenant_id, node_key: $object_name}) "
            f"MERGE (a)-[r:{relation_type} {{tenant_id: $tenant_id}}]->(b) "
            "SET r.source = $source, r.provenance = $provenance, "
            "r.recorded_at = $recorded_at"
        )
        async with self._driver.session() as session:
            await session.run(
                query,
                {
                    "subject_name": subject_standard_name,
                    "object_name": object_standard_name,
                    "source": source,
                    "tenant_id": tenant_id,
                    "provenance": provenance,
                    "recorded_at": recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        """删除某个文档、某个租户抽取出的全部关系边，重新摄取该文档前调用。

        tenant_id 是必填过滤条件——不同租户即使摄取了相同相对路径的文档
        （source 字符串相同），也只会删自己那部分边，不会互相影响。
        """
        async with self._driver.session() as session:
            await session.run(
                _DELETE_RELATIONS_BY_SOURCE_QUERY,
                {"source": source, "tenant_id": tenant_id},
            )

    async def count_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> tuple[int, int]:
        """返回 (陈旧边条数, 该源文件下 ETL 边总数)，供关系侧的安全阀判定用。

        过滤条件跟 delete_stale_relations_by_source 逐字一致——两者必须看到
        同一批边，否则阀判的和实际删的就不是一回事。

        调用时机是"关系已写完、陈旧边还没删"：此时总数 = 本轮重写的 + 陈旧的。
        源文件被误传或截断时本轮重写得少、陈旧的多，比值因此升高。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _COUNT_STALE_RELATIONS_QUERY,
                {
                    "source": source,
                    "tenant_id": tenant_id,
                    "provenance": provenance.ETL,
                    "before": before_recorded_at,
                },
            )
            record = await result.single()
            if record is None:
                return (0, 0)
            return (record["stale"], record["total"])

    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int:
        """删除某个源文件下、本次 ETL 运行没有重写过的关系边，返回删除条数。

        与 delete_relations_by_source（全删）的区别是它只删陈旧的那些——
        配合"先写新边、再扫陈旧边"的顺序，图谱在任何时刻都是完整的，见
        _DELETE_STALE_RELATIONS_QUERY 的说明。"""
        async with self._driver.session() as session:
            result = await session.run(
                _DELETE_STALE_RELATIONS_QUERY,
                {
                    "source": source,
                    "tenant_id": tenant_id,
                    "provenance": provenance.ETL,
                    "before": before_recorded_at,
                },
            )
            record = await result.single()
            return record["removed"] if record else 0

    async def sync_term(self, term: Term) -> None:
        """把术语表里的一个标准术语同步进图谱：写入/更新标准节点的
        type 属性，并为每个别名建一个独立节点通过 ALIAS_OF
        指向标准节点——对应架构文档 §4.1"别名作为独立节点"的设计，是
        术语表（基准真相）到图谱的同步步骤，与 merge_relation（写入 LLM
        抽取出的关系边）是两条独立的写入路径。
        """
        async with self._driver.session() as session:
            await session.run(
                _SYNC_TERM_QUERY,
                {
                    "tenant_id": term.tenant_id,
                    "node_key": term.node_key,
                    "standard_name": term.standard_name,
                    "type": term.term_type,
                    "aliases": list(term.aliases),
                    "extra_properties": term.extra_properties,
                },
            )

    async def sync_terms(self, terms: list[Term]) -> None:
        for term in terms:
            await self.sync_term(term)

    async def count_relation_edges_for_term(self, *, tenant_id: str, node_key: str) -> int:
        """统计该术语节点参与的、非结构性同步边（ALIAS_OF）的关系边数量，
        供管理后台删除术语前的守卫检查用——见 _COUNT_TERM_RELATION_EDGES_QUERY
        的说明。"""
        async with self._driver.session() as session:
            result = await session.run(
                _COUNT_TERM_RELATION_EDGES_QUERY,
                {"tenant_id": tenant_id, "node_key": node_key},
            )
            rows = await result.data()
            return rows[0]["edge_count"] if rows else 0

    async def probe_relation_fanout(
        self,
        *,
        tenant_id: str,
        relation_type: str,
        from_term_type: str,
        to_term_type: str,
        direction: str,
    ) -> int:
        """单个 from_term_type 节点沿这条关系最多能走到几个不同的
        to_term_type 节点。返回 > 1 表示这一跳不是函数关系。

        direction="outgoing" 表示边从 from 指向 to，"incoming" 表示反向——
        跟 structured_filter_query.Hop.direction 的取值一致。

        没有任何匹配边时返回 0：Cypher 的 max() 在空输入上返回 null，仍然会
        给出一行，不能把 None 直接透出去。
        """
        arrow_left, arrow_right = ("-", "->") if direction == "outgoing" else ("<-", "-")
        query = _FANOUT_QUERY_TEMPLATE.format(
            arrow_left=arrow_left, arrow_right=arrow_right, relation_type=relation_type,
        )
        async with self._driver.session() as session:
            result = await session.run(
                query,
                {
                    "tenant_id": tenant_id,
                    "from_term_type": from_term_type,
                    "to_term_type": to_term_type,
                },
            )
            rows = await result.data()
        if not rows:
            return 0
        return rows[0]["fanout"] or 0

    async def rename_term_node(
        self, *, tenant_id: str, node_key: str, new_standard_name: str
    ) -> None:
        """把一个术语节点的 standard_name 属性原地改成新值，不影响节点
        已有的关系边、也不改变 node_key——见 _RENAME_TERM_NODE_QUERY 的
        说明。调用方必须自己先确认 new_standard_name 不会跟同租户下另一个
        已存在的术语节点冲突。"""
        async with self._driver.session() as session:
            await session.run(
                _RENAME_TERM_NODE_QUERY,
                {
                    "tenant_id": tenant_id,
                    "node_key": node_key,
                    "new_standard_name": new_standard_name,
                },
            )

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        """删除一个术语节点及其别名节点——只应该在确认过
        count_relation_edges_for_term() 返回 0 之后调用，见
        _DELETE_TERM_NODE_QUERY 的说明。"""
        async with self._driver.session() as session:
            await session.run(
                _DELETE_TERM_NODE_QUERY, {"tenant_id": tenant_id, "node_key": node_key}
            )

    async def ensure_tenant_scoped_schema(self) -> None:
        """建按租户/节点键、按租户/分类的属性索引，并把存量（本次改造前
        写入、没有 tenant_id/node_key 属性的）:Term 节点回填成
        tenant_id='default'——与 SQLite 侧 terms 表的迁移是同一次改造的
        两半，缺一半就会出现"SQLite 里租户隔离了，Neo4j 里还是老样子"
        的不一致状态。幂等，可在每次进程启动时调用。
        """
        async with self._driver.session() as session:
            for query in _ENSURE_INDEXES_QUERIES:
                await session.run(query)
            await session.run(_BACKFILL_LEGACY_TERM_NODES_QUERY)

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

        索引不显式命名（匿名 CREATE INDEX IF NOT EXISTS）——term_type 是分类枚举值
        （TermTypeWriteRequest.value），没有经过任何字符集校验，如果拼进索引名字符串
        里，一个带空格/标点的分类名会拼出格式非法的 CREATE INDEX 语句、把这个本该
        只是声明分类的路由变成未处理的 500。改成匿名索引后 term_type 就不再出现在
        Cypher 语句文本里的任何位置（ON 子句里的 t.type 是固定的属性名字面量，不是
        term_type 的值）——IF NOT EXISTS 的幂等性不依赖显式索引名，Neo4j 按标签+属性
        列表匹配已有索引定义，同一个 (tenant_id, type, field) 三元组重复调用一样会
        no-op。
        """
        _SCALAR_VALUE_TYPES = {"string", "number", "integer"}
        async with self._driver.session() as session:
            for spec in extra_fields:
                if spec.value_type not in _SCALAR_VALUE_TYPES:
                    continue
                await session.run(
                    f"CREATE INDEX IF NOT EXISTS FOR (t:Term) ON "
                    f"(t.tenant_id, t.type, t.{spec.name})"
                )

    async def migrate_relation_type_edges(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int:
        """把某个租户所有旧类型的边批量改成新类型，返回迁移的边数。

        Neo4j 的关系类型（edge type）一旦写入不可原地修改——"改名"只能新建一条
        新类型的边、把原边的全部属性复制过去、再删掉旧边，这是一次真正的数据
        迁移，不是字符串替换（见 app/graphrag/ontology_relations.py 改名逻辑的
        说明）。这是租户级自定义关系类型改名后，业务显式触发的可选操作——不改名
        的旧边永远留着旧类型字符串也完全可用（query_subgraph 的 1 跳查询不按
        类型过滤），触发这个方法只是为了让图谱里同一语义不再同时存在新旧两种
        类型字符串。

        单条 Cypher 语句一次性处理该租户全部旧类型的边，不做分批——当前没有
        证据支撑单租户单次改名会涉及大量边到需要分批的程度，等真实场景出现
        性能问题再引入分批处理（YAGNI）。
        """
        if not _RELATION_TYPE_NAME_PATTERN.match(old_type):
            raise ValueError(f"旧关系类型名字不合法: {old_type!r}")
        if not _RELATION_TYPE_NAME_PATTERN.match(new_type):
            raise ValueError(f"新关系类型名字不合法: {new_type!r}")
        query = (
            f"MATCH (a)-[r:{old_type} {{tenant_id: $tenant_id}}]->(b) "
            "WITH a, b, r, properties(r) AS props "
            f"CREATE (a)-[r2:{new_type}]->(b) "
            "SET r2 = props "
            "WITH r, r2 "
            "DELETE r "
            "RETURN count(r2) AS migrated_count"
        )
        async with self._driver.session() as session:
            result = await session.run(query, {"tenant_id": tenant_id})
            rows = await result.data()
        return rows[0]["migrated_count"] if rows else 0

    async def migrate_term_type_nodes(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int:
        """把某个租户所有旧 term_type 的 :Term 节点属性批量改成新值，返回
        迁移的节点数。term_type 是参数化传入的节点属性值（t.type），不是
        像关系类型那样拼进 Cypher 结构本身——不需要 migrate_relation_type_edges
        那套正则白名单校验防注入，也不需要"建新边、复制属性、删旧边"的
        重建套路，原地 SET 一下就行。
        """
        query = (
            "MATCH (t:Term {tenant_id: $tenant_id, type: $old_type}) "
            "SET t.type = $new_type "
            "RETURN count(t) AS migrated_count"
        )
        async with self._driver.session() as session:
            result = await session.run(
                query, {"tenant_id": tenant_id, "old_type": old_type, "new_type": new_type}
            )
            rows = await result.data()
        return rows[0]["migrated_count"] if rows else 0
