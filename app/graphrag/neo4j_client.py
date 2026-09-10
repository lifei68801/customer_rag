from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.graphrag.date_normalization import is_normalized_date
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

logger = logging.getLogger(__name__)

#: 关系边的对端节点没有 type 属性时，影响面分项里给它的名字。分项之和必须
#: 等于总数，所以这一类不能丢掉、只能有个名字。
UNKNOWN_COUNTERPART_TYPE = "未标类型"

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
#
# 对端节点 (related:Term {tenant_id: $tenant_id}) 也要按租户匹配，不能只过滤
# 起点和边：一条「边标着本租户、对端节点属别的租户」的边能通过 r.tenant_id
# 这道过滤，对端的 standard_name 就会被列进管理界面。这是纵深防御，不是在
# 修一处正在发生的泄漏——正常写入路径产生不了这种边（merge_relation 给两端
# 节点和边用的是同一个 $tenant_id 参数），但早期示例数据留下过租户标记不一致
# 的脏边（见 _SURVEY_INCONSISTENT_RELATION_EDGES_QUERY 的两类），所以查询
# 不再依赖数据干净。
_TERM_RELATIONS_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term {tenant_id: $tenant_id})
WHERE r.tenant_id = $tenant_id
RETURN CASE WHEN startNode(r) = t THEN 'out' ELSE 'in' END AS direction,
       type(r) AS relation_type,
       related.node_key AS node_key,
       related.standard_name AS standard_name,
       related.type AS term_type
ORDER BY relation_type, standard_name
"""

_SUBGRAPH_ONE_HOP_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term {tenant_id: $tenant_id})
WHERE r.tenant_id = $tenant_id
RETURN related.standard_name AS related_name, type(r) AS relation_type, 1 AS hops
"""

_SUBGRAPH_TWO_HOP_QUERY_TEMPLATE = """
MATCH p = (t:Term {{tenant_id: $tenant_id, node_key: $node_key}})-[r:{relation_types}*2..2]-(related:Term {{tenant_id: $tenant_id}})
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id)
  AND ALL(n IN nodes(p) WHERE n.tenant_id = $tenant_id)
  AND related <> t
RETURN related.standard_name AS related_name,
       [rel IN r | type(rel)][-1] AS relation_type,
       2 AS hops
"""
# 第二段 UNION 只对"链式"关系放开到恰好 2 跳（*2..2，不是 *1..2，避免和
# 第一段的 1 跳结果重复）——前提链、流程顺序、包含层级经常需要连续追问
# 两步；其余关系类型语义上查 1 跳就有意义，继续放开多跳容易发散、引入
# 噪声上下文。
#
# 哪些关系类型算"链式"由租户自己在管理后台勾选（tenant_relation_types.
# allow_chain_query），调用方查出来后经 query_subgraph 的
# chain_query_relation_types 参数传进来，不再写死 REQUIRES/PRECEDES/
# PART_OF——写死的那版让界面上的「支持链式查询」复选框跟实际检索行为完全
# 脱钩：勾上自定义关系没有任何效果，取消勾选默认关系也照样两跳。
#
# 两段 UNION 的对端节点都写成 (related:Term {tenant_id: $tenant_id})，理由同
# _TERM_RELATIONS_QUERY 的说明——区别只在泄漏的去向：这条查询的结果直接进
# LLM 的检索上下文，别的租户的 standard_name 会出现在回答里。
#
# 第二段的 ALL(n IN nodes(p) WHERE n.tenant_id = $tenant_id) 管的是路径中间
# 那个节点：*2..2 的路径上除了两端还有一个中间节点，只给终点加
# {tenant_id: $tenant_id} 是管不到它的。nodes(p) 覆盖路径上的全部节点（起点、
# 中间、终点），所以这一条本身就把终点也校验了；终点上那份 {tenant_id: ...}
# 保留着是让匹配阶段就收窄，也让「对端按租户过滤」这件事在模式里一眼可见。
# 为此把这一段改成具名路径 MATCH p = (...)——nodes() 要拿路径，只有关系列表 r
# 是不够的。
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


def _safe_chain_relation_types(chain_query_relation_types: set[str]) -> list[str]:
    """把链式关系类型过一遍格式校验，返回可以拼进 Cypher 的那些（已排序）。

    关系类型没法参数化绑定，只能拼进查询文本，所以拼之前必须再过一次
    `ontology_relations._RELATION_TYPE_PATTERN` 那份格式校验——数据是从
    SQLite 读出来的，写入路径校验过不等于读出来就能免检。不合格的丢掉并
    记日志，不让它进 Cypher。

    抽成函数是因为**两条查询要用同一份**：子图查询（问答的两跳上下文）和
    邻域图查询（图谱预览页）。各写各的话，用户在预览里看到 A 两跳能到 C、
    回去问却答不出来——而他会拿这两处互相印证。
    """
    safe_types = sorted(
        rt for rt in chain_query_relation_types if _RELATION_TYPE_NAME_PATTERN.match(rt)
    )
    rejected = sorted(set(chain_query_relation_types) - set(safe_types))
    if rejected:
        logger.warning(
            "查询跳过了 %d 个格式不合法的链式关系类型（不会拼进 Cypher）：%s",
            len(rejected),
            "、".join(rejected),
        )
    return safe_types


_NEIGHBORHOOD_ONE_HOP_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term {tenant_id: $tenant_id})
WHERE r.tenant_id = $tenant_id AND type(r) <> 'ALIAS_OF'
WITH DISTINCT r
WITH r, startNode(r) AS s, endNode(r) AS o
RETURN s.node_key AS source_node_key, s.standard_name AS source_name, s.type AS source_type,
       type(r) AS relation_type,
       o.node_key AS target_node_key, o.standard_name AS target_name, o.type AS target_type
"""

_NEIGHBORHOOD_TWO_HOP_QUERY_TEMPLATE = """
MATCH p = (t:Term {{tenant_id: $tenant_id, node_key: $node_key}})-[r:{relation_types}*2..2]-(related:Term {{tenant_id: $tenant_id}})
WHERE ALL(rel IN r WHERE rel.tenant_id = $tenant_id)
  AND ALL(n IN nodes(p) WHERE n.tenant_id = $tenant_id)
  AND related <> t
UNWIND r AS edge
WITH DISTINCT edge
WITH edge, startNode(edge) AS s, endNode(edge) AS o
RETURN s.node_key AS source_node_key, s.standard_name AS source_name, s.type AS source_type,
       type(edge) AS relation_type,
       o.node_key AS target_node_key, o.standard_name AS target_name, o.type AS target_type
"""
# 邻域图：给「图谱预览」页画图用，跟 _SUBGRAPH_* 那两条是两码事。
#
# 那两条 RETURN 的是 related_name / relation_type / hops——给 agent 拼文本
# 上下文够用，画图不够：没有 node_key（前端点开一个邻居继续展开时没有可用的
# 标识，而 standard_name 在同一租户里可以重名）、没有 term_type（按类型上色
# 是这一页最基本的可读性），而且**不知道每条边连的是哪两个点**——两跳那条
# 只返回终点名和最后一跳的关系类型，中间节点整个丢失，拿它画出来的是一堆从
# 中心射出去的假边：一张看起来正常、拓扑却是错的图。
#
# 两跳这条 UNWIND 出路径上的**每一条边**，所以中间节点会自然出现在结果里。
# WITH DISTINCT edge 去重：同一条边会被多条路径命中。
# 方向用边自己的 startNode/endNode 还原，不按遍历方向报。
#
# 排除 ALIAS_OF：别名边是词表→图谱的结构性同步边，不是知识图谱数据，
# 画在图上只会让每个实体多出一串没有意义的卫星点。


def _build_neighborhood_query(chain_query_relation_types: set[str]) -> str:
    """邻域图查询。消毒逻辑跟 _build_subgraph_query 共用同一个函数。

    一个合格的链式关系类型都没有时只查一跳——`[r:*2..2]` 会匹配所有关系
    类型，是比"固定几种"更糟的无差别两跳发散，在预览页上表现为一张糊掉的图。
    """
    safe_types = _safe_chain_relation_types(chain_query_relation_types)
    if not safe_types:
        return _NEIGHBORHOOD_ONE_HOP_QUERY
    two_hop = _NEIGHBORHOOD_TWO_HOP_QUERY_TEMPLATE.format(
        relation_types="|".join(safe_types)
    )
    return f"{_NEIGHBORHOOD_ONE_HOP_QUERY}" + "\nUNION\n" + two_hop


def _build_subgraph_query(chain_query_relation_types: set[str]) -> str:
    """按租户放开链式查询的关系类型拼出子图查询。

    关系类型没法参数化绑定，只能拼进查询文本，所以拼之前必须再过一次
    ontology_relations._RELATION_TYPE_PATTERN 那份格式校验——数据是从
    SQLite 读出来的，写入路径校验过不等于读出来就能免检。不合格的丢掉
    并记日志，不让它进 Cypher。

    一个合格的链式关系类型都没有时，整段 UNION 不拼：`[r:*2..2]` 会匹配
    所有关系类型，是比"固定三种"更糟的无差别两跳发散。
    """
    safe_types = _safe_chain_relation_types(chain_query_relation_types)
    if not safe_types:
        return _SUBGRAPH_ONE_HOP_QUERY
    two_hop = _SUBGRAPH_TWO_HOP_QUERY_TEMPLATE.format(
        relation_types="|".join(safe_types)
    )
    return f"{_SUBGRAPH_ONE_HOP_QUERY}\nUNION\n{two_hop}"


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

_DELETE_RELATION_EDGE_QUERY = """
MATCH (a:Term {tenant_id: $tenant_id, node_key: $subject_node_key})-[r]->(b:Term {tenant_id: $tenant_id, node_key: $object_node_key})
WHERE type(r) = $relation_type AND r.tenant_id = $tenant_id
DELETE r
RETURN count(r) AS removed
"""
# 按业务键定位一条边：起点 node_key + 关系类型 + 终点 node_key + 租户。
# Neo4j 的内部关系 id 不稳定（重建/恢复后会变），不能拿来当外部句柄。
#
# 有向模式：(a)-[r]->(b) 和 (b)-[r]->(a) 是两条不同的边，无向匹配会让
# “删掉 A 指向 B 的那条”顺手把 B 指向 A 的那条也删了。同一对节点、同一
# 类型、同一租户下如果存在多条平行边（merge_relation 的 MERGE 语义不会
# 产生，但历史数据里可能有），这条语句会把它们一起删掉并如实返回条数——
# 业务键在这个粒度上不区分它们，删一半留一半反而是更差的结果。
#
# 关系类型走 $relation_type 参数（WHERE type(r) = ...）而不是插值进模式：
# 这个值来自 HTTP 请求，本文件里其它做插值的地方（execute_structured_filter_
# query / probe_relation_fanout）都以“调用方已跑过白名单校验”为前提，删边
# 这条路径没有那样一份白名单。
#
# r.tenant_id = $tenant_id 同 _TERM_RELATIONS_QUERY：两端节点属于
# 本租户、边却标着别的租户的历史脏数据是真实存在的，删除路径不能顺手动
# 别的租户的边。
#
# DELETE 之后 RETURN count(r) 统计的是本次匹配+删除的行数，不是删除后剩余
# 的边数——这个语义已经在 _DELETE_STALE_RELATIONS_QUERY 上用真实 Neo4j
# 5.22 验证过（见该查询的说明）。

_LIST_TENANT_DIRTY_EDGES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id})-[r]-(related:Term)
WHERE type(r) <> 'ALIAS_OF'
  AND (r.tenant_id IS NULL
       OR related.tenant_id IS NULL
       OR r.tenant_id <> t.tenant_id
       OR related.tenant_id <> t.tenant_id)
WITH DISTINCT r
WITH r, startNode(r) AS subject, endNode(r) AS object
RETURN subject.node_key AS subject_node_key,
       subject.standard_name AS subject_standard_name,
       type(r) AS relation_type,
       object.node_key AS object_node_key,
       object.standard_name AS object_standard_name,
       r.tenant_id AS edge_tenant_id,
       subject.tenant_id AS subject_tenant_id,
       object.tenant_id AS object_tenant_id
ORDER BY subject_node_key, relation_type, object_node_key
LIMIT $limit
"""
# 整个租户的脏边，不锚在某一个实体上。
#
# 这一页存在的理由就是那个"不锚定"：脏边的列举此前只在实体详情页里，你得
# **先知道是哪个实体**才看得到它的脏边——而脏边的特点恰恰是没人知道它们在
# 哪。运维只能一个实体一个实体点过去。
#
# 判定条件跟 _LIST_INCONSISTENT_RELATION_EDGES_QUERY 逐字一致（边的 tenant_id
# 为空、或跟两端对不上、或对端节点跨租户），只是去掉 node_key 这个锚点。
# 口径不一致的话，全局页列出来的和详情页列出来的对不上——运维在全局页删完，
# 点进那个实体一看还有。
#
# **无向匹配**，跟详情页那条一致。
#
# 曾经只走出边，理由写的是"两端都在扫描范围内，无向就是重复"——那个前提对
# 脏边恰恰不成立：判据本身就是「对端跨租户 / 对端没有租户标记」，那种边的
# 对端根本不是本租户的 Term，不在 (t:Term {tenant_id: $tenant_id}) 的扫描
# 范围里。于是"本租户节点作宾语、主语属于别人"的脏边永远列不出来，而实体
# 详情页那条无向查询看得见它——同一条边一个页面有、一个页面没有，正是这段
# 注释下面警告过的那个后果。
#
# 两端都在本租户时会各命中一次，靠 WITH DISTINCT r 去重；方向用边自己的
# startNode/endNode 还原，而不是按遍历方向报——否则同一条边从哪一端遍历到，
# 主宾就反过来。
#
# 从节点侧起手的理由同 _COUNT_TENANT_RELATION_EDGES_QUERY：不是走索引
# （复合索引只给 tenant_id 用不上），是扫描量级——节点侧扫这一个租户的
# Term，关系侧扫全库所有租户的所有边。


_LIST_INCONSISTENT_RELATION_EDGES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})-[r]-(related:Term)
WHERE type(r) <> 'ALIAS_OF'
  AND (r.tenant_id IS NULL
       OR related.tenant_id IS NULL
       OR r.tenant_id <> t.tenant_id
       OR related.tenant_id <> t.tenant_id)
RETURN CASE WHEN startNode(r) = t THEN 'out' ELSE 'in' END AS direction,
       type(r) AS relation_type,
       related.node_key AS node_key,
       related.standard_name AS standard_name,
       related.type AS term_type,
       related.tenant_id AS other_tenant_id,
       r.tenant_id AS edge_tenant_id
ORDER BY relation_type, node_key
"""
# _TERM_RELATIONS_QUERY 的补集：那条按 r.tenant_id = $tenant_id 过滤，于是
# 租户标记异常的边在详情页上根本不出现——用户找不到它，也就无从删起，而
# 它照样挂在节点上。这条把同一个节点身上的那批边单独列出来。
#
# 「两端节点跨租户、边自己标着本租户」这一类曾经**会**出现在那份正常清单里：
# _TERM_RELATIONS_QUERY 的 r.tenant_id = $tenant_id 通得过，而当时 related
# 那一端并不按租户过滤，于是另一个租户的节点会被列出来（同一个口子在
# _SUBGRAPH_QUERY 上意味着它还会进检索上下文）。现在两条查询的对端节点都
# 按租户匹配了，这类边跟其余脏边一样从正常清单里消失——于是更需要这份清单：
# 它是这些边在界面上唯一的出口。它们也删不掉：_DELETE_RELATION_EDGE_QUERY
# 把两端都钉死在同一个租户上，匹配不到跨租户的那条边。
#
# 起点仍然按 {tenant_id, node_key} 锁定：列的是"我这个实体身上挂着的脏
# 边"，不是全库的脏边——后者是运维的事，走启动时那条普查告警。
#
# 三个 IS NULL 分支不是冗余：Cypher 里 null <> 'x' 求值为 null（不是
# true），只写 <> 的话 tenant_id 缺失的那些边会被静默漏掉，而它们正是
# 这次要暴露的对象之一。
#
# 两端各自的租户和边自己的租户都要返回：前端要靠它们说清这条边到底哪儿
# 不对，删除时也要靠对端节点的租户去定位那条边。

_DELETE_INCONSISTENT_RELATION_EDGE_QUERY = """
MATCH (a:Term {tenant_id: $subject_tenant_id, node_key: $subject_node_key})-[r]->(b:Term {tenant_id: $object_tenant_id, node_key: $object_node_key})
WHERE type(r) = $relation_type
  AND type(r) <> 'ALIAS_OF'
  AND (r.tenant_id IS NULL
       OR a.tenant_id <> b.tenant_id
       OR r.tenant_id <> a.tenant_id)
DELETE r
RETURN count(r) AS removed
"""
# 删脏边。_DELETE_RELATION_EDGE_QUERY 按边的 tenant_id 过滤，而这些边的
# tenant_id 恰恰就是错的那个属性，所以它们删不掉——本方法改用两端节点
# 各自的租户定位，节点的租户是更可靠的判据（边的租户已经被证明会说谎）。
#
# 最后那个 WHERE 分支是安全边界，不是优化：不按边的 tenant_id 过滤之后，
# 如果不写死"只删违反不变式的边"，这条语句就成了一个能删任意边的后门。
# 三个分支恰好覆盖回填故意不碰的那两类（边没有租户 / 两端跨租户 / 边的
# 租户和起点对不上），健康的边一条都匹配不上。
#
# 谁有权调它由路由层判定（见 admin_terms_routes.py 的删除路由）：起点
# 节点固定取 URL 里那个已经过 require_tenant_access 校验的租户，所以
# member 借这条路径也只能碰到自己有权访问的租户节点身上的边；两端节点分属不同租户
# 的那一类另外要求平台管理员。
#
# 有向模式、关系类型走参数、DELETE 后 count(r) 的语义，理由全同
# _DELETE_RELATION_EDGE_QUERY。

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

_COUNT_TENANT_RELATION_EDGES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id})-[r]->()
WHERE r.tenant_id = $tenant_id AND type(r) <> 'ALIAS_OF'
RETURN count(r) AS edge_count
"""
# 看板上「这个领域有多少关系」。
#
# 从节点侧发起而不是 ()-[r]->()。理由**不是**"走索引"：这个库里只有
# (tenant_id, node_key) 和 (tenant_id, type) 两条复合索引，而 Neo4j 的复合
# 索引要求查询覆盖索引里的全部属性才用得上，只给 tenant_id 走不了任何一条
# （曾经在这里写过"能走它"，那是错的）。
#
# 真正的理由是扫描量级：从节点侧起手扫的是 :Term 标签下的节点、再各自展开
# 自己的出边，总量是**这一个租户**的边数；以 ()-[r]->() 起手扫的是全库
# 所有租户的所有边。节点数（MUJI 的 SKU 18 万量级）本来就比边数（百万级）
# 小一个量级，何况后者还跨租户。
#
# 想让它真的走索引，需要一条 (t.tenant_id) 单属性索引——这个环境连不上
# Neo4j，加了也验不了效果，所以只把选项记在这里。
#
# 只数出边（-[r]->()）：无向匹配会让每条边被两端各数一次，看板上的数字直接
# 翻倍，而用户拿它跟实体详情页数出来的边核对时会发现对不上。
#
# 过滤口径跟 _TERM_RELATIONS_QUERY / _SUMMARIZE_TERM_RELATION_EDGES_QUERY
# 一致：r.tenant_id 过滤 + 排除 ALIAS_OF。别名边是词表→图谱的结构性同步边，
# 不是知识图谱数据。


_SUMMARIZE_TERM_RELATION_EDGES_QUERY = """
UNWIND $node_keys AS nk
MATCH (t:Term {tenant_id: $tenant_id, node_key: nk})-[r]-(other)
WHERE r.tenant_id = $tenant_id AND type(r) <> 'ALIAS_OF'
WITH r, head(collect(other)) AS counterpart
RETURN coalesce(counterpart.type, $unknown_label) AS counterpart_type,
       count(r) AS edge_count
ORDER BY edge_count DESC, counterpart_type
"""
# 删实体之前算影响面：这批实体一旦删掉，会连带删掉多少条关系边、这些边
# 主要连向哪几类实体。删除本身走 DETACH DELETE，边是一定会跟着节点走的
# （见 _DELETE_TERM_NODES_QUERY），所以这个数字不是"可能受影响"，是
# "一定会没"——用户在按下确认之前必须看到它。
#
# 过滤口径跟 _TERM_RELATIONS_QUERY（详情页列出的边）一致：r.tenant_id 过滤 +
# 排除 ALIAS_OF + 无向匹配。预演报的数和用户在详情页数得出来的数对不上的
# 话，他会认为其中一个在说谎，而他没有办法判断是哪个。
#
# 排除 ALIAS_OF：别名边是术语表→图谱的结构性同步边（sync_term 写入，见
# _SYNC_TERM_QUERY），不是知识图谱数据。把它算进"会连带删掉的关系边"里，
# 每个有别名的实体都会凭空多报几条。
#
# 无向匹配：入边和出边都会被 DETACH DELETE 带走，只数一个方向就是少报。
#
# WITH r, head(collect(other)) 是按边去重的关键，它一次解决两个重复：自环
# (t)-[r]-(t) 在无向模式下匹配两次；一条边的两端都在待删列表里时，它会分别
# 以两个 nk 各匹配一次。按 r 分组之后每条边只剩一行，各类型的计数加起来
# 正好等于"会被删掉的边总数"——分项和总数对不上是这里最容易出的错，而
# 用户会拿它们互相验算。head(collect(...)) 取哪一端在"两端都要删"这种情况
# 下是任意的，但那时两端都会消失，归给谁都不影响总数。
#
# counterpart 可能没有 type 属性（历史数据里写进过别的形状），此时用调用方
# 传进来的 $unknown_label 兜底——分项里少一块的话总数就对不上了。
#
# 这个数仍然可能少于实际消失的边数：DETACH DELETE 不带任何过滤，租户标记
# 异常的历史脏边（见 _BACKFILL_LEGACY_RELATION_EDGES_QUERY 的说明）也会
# 跟着走，而这条查询按 r.tenant_id 过滤，看不见它们。宁可少报——那批边在
# 界面上从来就无从查证，把它们算进用户要确认的代价里只会让这个数字无法核对。

_DELETE_TERM_NODES_QUERY = """
UNWIND $node_keys AS node_key
MATCH (t:Term {tenant_id: $tenant_id, node_key: node_key})
OPTIONAL MATCH (a:Term)-[:ALIAS_OF]->(t)
DETACH DELETE t, a
"""
# _DELETE_TERM_NODE_QUERY 的批量版，语义逐字相同（连别名节点一起删），
# 只是把 node_key 换成 UNWIND 的一个列表参数。

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

_NON_ISO_DATE_VALUES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, type: $term_type})
WHERE t[$field] IS NOT NULL
RETURN DISTINCT t[$field] AS value
"""
# 确认本体前扫存量日期值（Task 7：count_non_iso_date_values）。field 走
# t[$field] 动态属性访问参数化，不拼进语句文本——经实测确认 Neo4j 5.22
# （docker-compose 里跑的版本）配 driver 6.2.0 支持这个语法：往一个临时
# 节点写入属性后用 t[$field] 参数化读回，确实拿到了写入的值。
#
# 不需要 execute_structured_filter_query 那种"插值换索引命中"的取舍——
# 那里插值是为了让规划器在结构化查询的 WHERE 里用上 (tenant_id, type,
# field) 复合索引；这里本来就要把该字段的全部去重值搬到 Python 侧逐个
# 跑 is_normalized_date，(tenant_id, type) 索引已经把扫描收窄到这一个
# term_type 下的节点，字段本身是否命中标量索引不影响这次要做的工作量。

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

_BACKFILL_LEGACY_RELATION_EDGES_QUERY = """
MATCH (a:Term)-[r]->(b:Term)
WHERE r.tenant_id IS NULL
  AND type(r) <> 'ALIAS_OF'
  AND a.tenant_id IS NOT NULL
  AND a.tenant_id = b.tenant_id
SET r.tenant_id = a.tenant_id
"""
# 节点回填的对称补丁：上面那条只 SET 节点，边上的 tenant_id 一直没人补。
# 旧库里因此可能仍有 tenant_id 为 null 的关系边——它们被详情页
# （_TERM_RELATIONS_QUERY）和删除影响面预演
# （_SUMMARIZE_TERM_RELATION_EDGES_QUERY）一致地忽略，同时又删不掉
# （_DELETE_RELATION_EDGE_QUERY 也按边的 tenant_id 过滤），于是它们成了
# 一批只存在于库里、界面上无从查证也无从处置的数据。（删实体时它们仍会被
# DETACH DELETE 连带删掉——那一步不带任何过滤；预演报的数因此可能少于
# 实际消失的边数，差额正好是这批脏边。）
#
# 只回填「两端节点同租户、边自己没有 tenant_id」这一类：这类边的归属
# 没有歧义，补的正是 merge_relation 写入时本就该有的那个值（写边时两端
# 节点的 MERGE 匹配属性和边上的 tenant_id 用的是同一个 $tenant_id 参数）。
#
# 另外两类脏边故意不动，只统计+告警（见 _SURVEY_INCONSISTENT_RELATION_
# EDGES_QUERY）：边的 tenant_id 与两端节点对不上（B 类）、两端节点分属
# 不同租户（C 类）。自动“修正”它们等于让一批今天被一致忽略的边突然活
# 过来参与检索与守卫——那是在悄悄改变租户隔离边界，必须由人来决定。
#
# WHERE r.tenant_id IS NULL 保证幂等：跑过一次之后这些边都有 tenant_id
# 了，再跑匹配不到任何行。
#
# type(r) <> 'ALIAS_OF'：别名边是术语表→图谱的结构性同步边（sync_term
# 写入），从来不带 tenant_id，也不参与租户语义（见
# _TERM_RELATIONS_QUERY 的同款说明）——给它补一个租户属性
# 等于凭空发明语义。

#: 告警里最多点名几条脏边。同 ontology_categories 的 _IN_USE_SAMPLE_SIZE：
#: 3 条够运维认出是哪批数据，再多没人读，剩下的用总数兜底。
_INCONSISTENT_EDGE_SAMPLE_SIZE = 3

_SURVEY_INCONSISTENT_RELATION_EDGES_QUERY = f"""
MATCH (a:Term)-[r]->(b:Term)
WHERE type(r) <> 'ALIAS_OF'
  AND a.tenant_id IS NOT NULL AND b.tenant_id IS NOT NULL
  AND (a.tenant_id <> b.tenant_id
       OR (r.tenant_id IS NOT NULL AND r.tenant_id <> a.tenant_id))
WITH CASE WHEN a.tenant_id <> b.tenant_id
          THEN 'cross_tenant' ELSE 'edge_tenant_mismatch' END AS category,
     {{subject_tenant_id: a.tenant_id, subject_node_key: a.node_key,
      relation_type: type(r),
      object_tenant_id: b.tenant_id, object_node_key: b.node_key,
      edge_tenant_id: r.tenant_id}} AS sample
RETURN category, count(*) AS total,
       collect(sample)[0..{_INCONSISTENT_EDGE_SAMPLE_SIZE}] AS samples
"""
# 上面那条回填故意不碰的两类脏边，在这里被数出来并告警——不改它们，但
# 绝不能让它们停在"既不参与检索、也删不掉、还没人知道它存在"的状态里。
#
# 两类的判据：
#   cross_tenant         两端节点分属不同租户，这条边本身就是跨租户的；
#   edge_tenant_mismatch 两端节点同租户，边自己标着另一个租户（真实库里
#                        出现过：两端 default、边 demo，早期 demo 数据的
#                        遗留），或者边标着租户而节点没有。
# tenant_id 为 null 的边不在这里——两端同租户的那些已经被上面那条回填补
# 好了，剩下的只会是这两类。
#
# CASE 的两个分支顺序不能反：跨租户的边同时也可能满足"边的租户和某一端
# 对不上"，先判跨租户是因为那是更严重、也更需要人工介入的那一类。


def _describe_inconsistent_edge(sample: dict[str, Any]) -> str:
    """一条脏边渲染成人能拿去查的样子：两端各自的租户和 node_key、关系
    类型、以及边自己标的租户。少任何一项，运维都没法在库里把它找出来。"""
    return (
        f"{sample.get('subject_tenant_id')}/{sample.get('subject_node_key')}"
        f" -{sample.get('relation_type')}-> "
        f"{sample.get('object_tenant_id')}/{sample.get('object_node_key')}"
        f"（边的 tenant_id={sample.get('edge_tenant_id')}）"
    )


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

    async def delete_term_nodes(
        self, *, tenant_id: str, node_keys: list[str]
    ) -> None: ...

    async def summarize_relation_edges_for_terms(
        self, *, tenant_id: str, node_keys: list[str]
    ) -> list[dict[str, Any]]: ...

    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int: ...

    async def query_neighborhood(
        self, node_key: str, *, tenant_id: str, chain_query_relation_types: set[str]
    ) -> list[dict[str, Any]]: ...

    async def list_tenant_dirty_edges(
        self, *, tenant_id: str, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool]: ...

    async def list_term_relations(
        self, *, tenant_id: str, node_key: str
    ) -> list[dict[str, Any]]: ...

    async def delete_relation_edge(
        self, *, tenant_id: str, subject_node_key: str, relation_type: str,
        object_node_key: str,
    ) -> int: ...

    async def list_inconsistent_relation_edges(
        self, *, tenant_id: str, node_key: str
    ) -> list[dict[str, Any]]: ...

    async def delete_inconsistent_relation_edge(
        self, *, subject_tenant_id: str, subject_node_key: str, relation_type: str,
        object_tenant_id: str, object_node_key: str,
    ) -> int: ...

    async def ensure_extra_field_indexes(
        self, *, tenant_id: str, term_type: str, extra_fields: list["ExtraFieldSpec"]
    ) -> None: ...

    async def migrate_relation_type_edges(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int: ...

    async def migrate_term_type_nodes(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int: ...

    async def count_non_iso_date_values(
        self, *, tenant_id: str, term_type: str, field: str
    ) -> tuple[int, list[str]]: ...


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

    async def query_neighborhood(
        self, node_key: str, *, tenant_id: str, chain_query_relation_types: set[str]
    ) -> list[dict[str, Any]]:
        """以这个实体为中心的邻域，**每一行是一条边**（两端都带 node_key /
        标准名 / 类型）。图谱预览页用。

        跟 query_subgraph 的区别见 _NEIGHBORHOOD_ONE_HOP_QUERY 上方的说明：
        那个返回的是给 agent 拼文本用的扁平清单，画不出图。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _build_neighborhood_query(chain_query_relation_types),
                {"node_key": node_key, "tenant_id": tenant_id},
            )
            return await result.data()

    async def query_subgraph(
        self, node_key: str, *, tenant_id: str, chain_query_relation_types: set[str]
    ) -> list[dict[str, Any]]:
        """chain_query_relation_types 必填、没有默认值：调用方必须显式给出该
        租户放开了链式查询的关系类型（tenant_relation_types.allow_chain_query
        = 1 且已确认）。给一个"回退到三种默认关系"的默认值会让注入路径没接好
        时悄悄按错的关系集合查两跳，跟这次要修的缺陷是同一类问题；漏传直接
        TypeError，立刻暴露。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _build_subgraph_query(chain_query_relation_types),
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

    async def summarize_relation_edges_for_terms(
        self, *, tenant_id: str, node_keys: list[str]
    ) -> list[dict[str, Any]]:
        """这批实体删掉会连带删掉哪些关系边，按对端实体类型分项。

        返回 [{"counterpart_type": 类型名, "edge_count": 条数}, ...]，按条数
        倒序。各项之和就是会被删掉的边总数（按边去重，见
        _SUMMARIZE_TERM_RELATION_EDGES_QUERY）。

        空列表直接返回、不发查询，同 delete_term_nodes。
        """
        if not node_keys:
            return []
        async with self._driver.session() as session:
            result = await session.run(
                _SUMMARIZE_TERM_RELATION_EDGES_QUERY,
                {
                    "tenant_id": tenant_id,
                    "node_keys": list(node_keys),
                    "unknown_label": UNKNOWN_COUNTERPART_TYPE,
                },
            )
            rows = await result.data()
            return [
                {"counterpart_type": row["counterpart_type"], "edge_count": row["edge_count"]}
                for row in rows
            ]

    async def list_tenant_dirty_edges(
        self, *, tenant_id: str, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool]:
        """整个租户的脏边，外加"是不是被截断了"。

        **多要一条**（LIMIT limit+1）来判断截断：正好要 limit 条的话，
        "刚好 500 条"和"超过 500 条"拿到的结果一模一样，而这两种情况要对
        运维说的话完全不同。默默少列的话他会以为脏边只有 500 条，清完那
        500 条就以为干净了。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _LIST_TENANT_DIRTY_EDGES_QUERY,
                {"tenant_id": tenant_id, "limit": limit + 1},
            )
            rows = await result.data()
        truncated = len(rows) > limit
        return [dict(row) for row in rows[:limit]], truncated

    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int:
        """这个租户图里有多少条关系边。看板用。

        无行时返回 0：空图上 Cypher 的 count 仍会给出一行，所以这一支正常
        走不到；但返回 None 会让看板显示「null 条关系」，防一手比事后查
        便宜。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _COUNT_TENANT_RELATION_EDGES_QUERY, {"tenant_id": tenant_id}
            )
            rows = await result.data()
            return rows[0]["edge_count"] if rows else 0

    async def delete_term_nodes(self, *, tenant_id: str, node_keys: list[str]) -> None:
        """批量删除术语节点及其别名节点，一次往返。

        空列表直接返回、不发查询：批量删除里"全被守卫挡住"是常见结果，
        那次请求不该在图谱上留下一次无意义的往返。
        """
        if not node_keys:
            return
        async with self._driver.session() as session:
            await session.run(
                _DELETE_TERM_NODES_QUERY,
                {"tenant_id": tenant_id, "node_keys": list(node_keys)},
            )

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
        """删除一个术语节点及其别名节点。节点身上的关系边会被
        DETACH DELETE 一起删掉（见 _DELETE_TERM_NODE_QUERY），调用方应该先用
        summarize_relation_edges_for_terms() 把这个代价告诉用户。"""
        async with self._driver.session() as session:
            await session.run(
                _DELETE_TERM_NODE_QUERY, {"tenant_id": tenant_id, "node_key": node_key}
            )

    async def delete_relation_edge(
        self,
        *,
        tenant_id: str,
        subject_node_key: str,
        relation_type: str,
        object_node_key: str,
    ) -> int:
        """删掉一条关系边，返回实际删掉的条数（0 表示没有匹配到）。

        调用方必须把 0 当成“这条边不在了”如实报出去，不能静默当成成功——
        用户点了删除、界面上那条却还在，比报错更难排查。定位方式和有向/
        租户过滤的理由见 _DELETE_RELATION_EDGE_QUERY 的说明。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _DELETE_RELATION_EDGE_QUERY,
                {
                    "tenant_id": tenant_id,
                    "subject_node_key": subject_node_key,
                    "relation_type": relation_type,
                    "object_node_key": object_node_key,
                },
            )
            rows = await result.data()
            return rows[0]["removed"] if rows else 0

    async def list_inconsistent_relation_edges(
        self, *, tenant_id: str, node_key: str
    ) -> list[dict[str, Any]]:
        """这个实体身上租户标记异常的边——详情页那份清单（list_term_relations）
        看不到的那些。见 _LIST_INCONSISTENT_RELATION_EDGES_QUERY。"""
        async with self._driver.session() as session:
            result = await session.run(
                _LIST_INCONSISTENT_RELATION_EDGES_QUERY,
                {"tenant_id": tenant_id, "node_key": node_key},
            )
            return await result.data()

    async def delete_inconsistent_relation_edge(
        self,
        *,
        subject_tenant_id: str,
        subject_node_key: str,
        relation_type: str,
        object_tenant_id: str,
        object_node_key: str,
    ) -> int:
        """删掉一条租户标记异常的边，返回实际删掉的条数（0 表示没匹配到）。

        跟 delete_relation_edge 一样，调用方必须把 0 如实报出去。两端节点
        按各自的租户定位，且只会匹配违反租户不变式的边——见
        _DELETE_INCONSISTENT_RELATION_EDGE_QUERY。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _DELETE_INCONSISTENT_RELATION_EDGE_QUERY,
                {
                    "subject_tenant_id": subject_tenant_id,
                    "subject_node_key": subject_node_key,
                    "relation_type": relation_type,
                    "object_tenant_id": object_tenant_id,
                    "object_node_key": object_node_key,
                },
            )
            rows = await result.data()
            return rows[0]["removed"] if rows else 0

    async def ensure_tenant_scoped_schema(self) -> None:
        """建按租户/节点键、按租户/分类的属性索引，并把存量（本次改造前
        写入、没有 tenant_id/node_key 属性的）:Term 节点回填成
        tenant_id='default'——与 SQLite 侧 terms 表的迁移是同一次改造的
        两半，缺一半就会出现"SQLite 里租户隔离了，Neo4j 里还是老样子"
        的不一致状态。幂等，可在每次进程启动时调用。

        节点之后还回填关系边：只补"两端节点同租户、边自己没有 tenant_id"
        这一类（见 _BACKFILL_LEGACY_RELATION_EDGES_QUERY）。边的租户和
        节点对不上、或者两端节点分属不同租户的脏边故意不动。
        """
        async with self._driver.session() as session:
            for query in _ENSURE_INDEXES_QUERIES:
                await session.run(query)
            await session.run(_BACKFILL_LEGACY_TERM_NODES_QUERY)
            # 边的回填必须排在节点回填之后：它读的是两端节点的 tenant_id，
            # 节点还没回填时那个值是 null，一条都匹配不上。
            await session.run(_BACKFILL_LEGACY_RELATION_EDGES_QUERY)
            await self._warn_about_inconsistent_relation_edges(session)

    async def _warn_about_inconsistent_relation_edges(
        self, session: Neo4jSessionProtocol
    ) -> None:
        """把回填故意不碰的两类脏边数出来，非零就告警。

        零条时一个字都不打：每次启动刷一条"一切正常"，真出问题那天这条
        警告就淹在噪音里没人看了。

        普查失败不阻断启动（它是诊断，不是请求路径的前提），但失败本身
        也要告警——静默跳过的话，"没有告警"就同时代表"库是干净的"和
        "普查根本没跑成"，而这两件事需要完全不同的处理。
        """
        try:
            result = await session.run(_SURVEY_INCONSISTENT_RELATION_EDGES_QUERY)
            rows = await result.data()
        except Exception:
            logger.warning(
                "普查租户标记异常的关系边失败——这次启动无法判断图谱里是否存在"
                "这类边，请手工执行 _SURVEY_INCONSISTENT_RELATION_EDGES_QUERY 确认",
                exc_info=True,
            )
            return
        for row in rows:
            total = row.get("total") or 0
            if not total:
                continue
            listed = "；".join(
                _describe_inconsistent_edge(sample) for sample in row.get("samples") or []
            )
            if row.get("category") == "cross_tenant":
                logger.warning(
                    "图谱里有 %d 条两端节点分属不同租户的关系边（%s）。这类边本身就"
                    "跨越了租户边界，而且不一定是「沉默」的：边自己标着其中一端的租户"
                    "时，那一端的子图查询会沿着它走过去，把另一个租户的节点带进检索"
                    "上下文（子图查询过滤边的 tenant_id，不过滤对端节点的租户）。"
                    "系统不会自动改动它们——把它们归到某个"
                    "租户名下等于让它们重新参与该租户的检索，那是在悄悄挪动隔离"
                    "边界。请人工确认后处理：在实体详情页的『租户标记异常的关系边』"
                    "一栏可以看到并删除它们（跨租户的这一类只有平台管理员能删）。",
                    total, listed,
                )
            else:
                logger.warning(
                    "图谱里有 %d 条 tenant_id 与两端节点对不上的关系边（%s）。这类边"
                    "今天既不参与检索（子图查询按边的 tenant_id 过滤），也不参与实体"
                    "删除守卫，却仍然挂在节点上。系统不会自动改正它们的 tenant_id"
                    "——那等于让一批被忽略的边突然活过来参与检索，是在悄悄改变租户"
                    "隔离边界。请人工确认后处理：在实体详情页的『租户标记异常的关系"
                    "边』一栏可以看到并删除它们。",
                    total, listed,
                )

    async def ensure_extra_field_indexes(
        self, *, tenant_id: str, term_type: str, extra_fields: list["ExtraFieldSpec"]
    ) -> None:
        """给某个 term_type 已确认的 string/number/integer/date 属性字段建 Neo4j property
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
        # date 也建索引：它在 Neo4j 里是字符串属性（字典序即时间序），
        # 范围过滤全指着这个索引。
        _SCALAR_VALUE_TYPES = {"string", "number", "integer", "date"}
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

    async def count_non_iso_date_values(
        self, *, tenant_id: str, term_type: str, field: str
    ) -> tuple[int, list[str]]:
        """这个租户这个类型下，该字段的值不是补零 ISO 的有几个，外加最多 3 个样例。

        判据是「已经是补零 ISO」，不是「能不能归一」：2026/1/15 能归一，但它
        此刻躺在图里的样子仍然会破坏字典序，放行等于把问题留在数据里。

        字段名不拼进 Cypher 文本，走 t[$field] 动态属性访问的参数化写法——
        见 _NON_ISO_DATE_VALUES_QUERY 上方注释，这个语法在本项目实际跑的
        Neo4j 版本上经过实测确认可用。

        判定逻辑放在 Python 侧用 is_normalized_date，不在 Cypher 里写正则：
        写两份「合格」的定义迟早会不一致。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _NON_ISO_DATE_VALUES_QUERY,
                {"tenant_id": tenant_id, "term_type": term_type, "field": field},
            )
            rows = await result.data()
        # 非字符串值（正常写入路径不会产生，但防御性地处理历史脏数据）一律
        # 算不合格：is_normalized_date 要求 str 输入，None 已经被查询的
        # IS NOT NULL 挡掉了。
        bad_values = [
            value for row in rows
            for value in (row["value"],)
            if not (isinstance(value, str) and is_normalized_date(value))
        ]
        samples = [str(value) for value in bad_values[:3]]
        return len(bad_values), samples
