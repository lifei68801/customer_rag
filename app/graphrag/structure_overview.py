"""图谱的总体结构：由什么构成、靠什么连起来、哪里的结构会骗人。

看板此前给的是六个规模数字（实体 / 关系 / 文档 / 表格行 / 待审 / 失效问题）。
它们回答"有多少"，答不了"这是个什么形状的图"——而后者才是判断建模对不对的
依据：

- **实体按类型的构成**：某一类异常膨胀是映射配错的信号（一列自由文本被建成
  600 个互不相关的实体，规模数字上只表现为"实体多了 600"）。
- **关系按类型的边数**："关系 31k"里，10000 条订单→产品和 30 条产品→公司
  是完全不同的两件事，总数把它们抹平了。
- **扇出风险**：一个主语沿某条关系连到多个宾语时，沿它做计数聚合会把归属
  放大。demo 的真实案例：产品→公司 扇出为 3，于是"某公司有多少订单"恒等于
  订单总数。本体层完全正常，只有真实数据能看出来。
- **孤立实体**：建出来了却一条边都没连上，导入报告一切正常，查询什么都查不到。

这些都不是新算出来的指标，是把已有的判据摆到落地页上——扇出探测原本只在
本体图那一页用（admin_ontology_routes 的 graph-overlay），用户要先点进去才
可能发现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_constraints import AllowedCombination, list_allowed_combinations
from app.graphrag.terms_store import count_terms_merged_by_term_type

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FanoutProbe:
    subject_term_type: str
    relation_type: str
    object_term_type: str
    #: 一个主语节点沿这条关系最多连到几个不同的宾语节点。
    #: None = 探测失败（图谱不可用、这个类型还没有节点），按"未知"处理。
    fanout: int | None


@dataclass(frozen=True)
class GraphStructure:
    #: [(实体类型, 条数)]，按条数降序。走合并视图，跟实体明细页口径一致。
    entity_types: list[tuple[str, int]]
    #: [(关系类型, 边数)]，按边数降序。
    relation_types: list[tuple[str, int]]
    #: 扇出 > 1 的那些边。扇出未知（None）的不算进来——把"查不到"显示成
    #: "有风险"是另一种撒谎。
    risks: list[FanoutProbe]
    #: 图里的实体数，以及其中至少连着一条关系边的实体数。
    graph_term_count: int
    connected_term_count: int


async def probe_constraint_fanout(
    graph_client, *, tenant_id: str, combinations: list[AllowedCombination]
) -> list[FanoutProbe]:
    """逐条探测每个允许组合在真实数据里的扇出度。

    单条失败不中断整体：图谱可能正在重建、某个类型还没有任何节点，这时该退回
    "未知"而不是让整个视图报错。

    逐条查询、不做批量：约束数量通常是个位数到几十条（demo 是 6 条）。
    """
    probes: list[FanoutProbe] = []
    for c in combinations:
        try:
            value = await graph_client.probe_relation_fanout(
                tenant_id=tenant_id,
                relation_type=c.relation_type,
                from_term_type=c.subject_term_type,
                to_term_type=c.object_term_type,
                direction="outgoing",
            )
        except Exception:
            logger.exception(
                "探测扇出失败：tenant=%r %s -%s-> %s",
                tenant_id, c.subject_term_type, c.relation_type, c.object_term_type,
            )
            value = None
        probes.append(
            FanoutProbe(
                subject_term_type=c.subject_term_type,
                relation_type=c.relation_type,
                object_term_type=c.object_term_type,
                fanout=value,
            )
        )
    return probes


async def collect_graph_structure(
    review_conn: aiosqlite.Connection, graph_client, *, tenant_id: str
) -> GraphStructure:
    """一个领域的图谱结构。

    **图谱查不通时不接异常，让它抛出去**——跟 collect_tenant_stats 同一个
    取舍：报 0 的话用户看到的是"这个图一条关系都没有"，一个看起来正常、实际
    是错的结论，他会据此去重跑一遍导入。调用方接住它，把这块渲染成"没算出来"。

    实体构成用**已确认**本体的约束做扇出探测：看板说的是现在跑着的这份图谱，
    草稿里的组合还没有数据。
    """
    entity_counts = await count_terms_merged_by_term_type(review_conn, tenant_id)
    combinations = await list_allowed_combinations(review_conn, tenant_id, status="confirmed")
    edge_counts = await graph_client.count_edges_by_relation_type(tenant_id=tenant_id)
    graph_term_count, connected_term_count = await graph_client.count_connected_terms(
        tenant_id=tenant_id
    )
    probes = await probe_constraint_fanout(
        graph_client, tenant_id=tenant_id, combinations=combinations
    )
    return GraphStructure(
        entity_types=sorted(entity_counts.items(), key=lambda kv: (-kv[1], kv[0])),
        relation_types=sorted(edge_counts.items(), key=lambda kv: (-kv[1], kv[0])),
        risks=[p for p in probes if p.fanout is not None and p.fanout > 1],
        graph_term_count=graph_term_count,
        connected_term_count=connected_term_count,
    )
