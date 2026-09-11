from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.terms_store import TermNotFoundError, get_term_merged_by_node_key

logger = logging.getLogger(__name__)

#: 一张图最多画几个节点。
#:
#: 300 是浏览器里 forceatlas2 布局还能在一两秒内收敛、且人眼还能分辨的量级。
#: demo 那张图里「产品:Beer」连着上千条边——不设上限的话这一页会卡死。
#:
#: 超出时**说出来**（truncated + total_nodes），不是默默少画：默默少画的话
#: 用户会对着一张 300 个邻居的图下「这个实体只连了 300 个东西」的结论。
GRAPH_PREVIEW_NODE_CAP = 300

router = APIRouter(
    prefix="/api/admin/{tenant_id}/graph-preview",
    dependencies=[Depends(deps.require_admin_session)],
)


class GraphNode(BaseModel):
    node_key: str
    standard_name: str
    term_type: str | None


class GraphEdge(BaseModel):
    source: str
    relation_type: str
    target: str


class NeighborhoodResponse(BaseModel):
    center: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    #: 节点数超过上限、图被截掉了一部分。
    truncated: bool
    #: **真实**总数，不是上限。截断之后仍然说得出"其实有多少个"。
    total_nodes: int
    shown_nodes: int


@router.get("/{node_key:path}", response_model=NeighborhoodResponse)
async def get_neighborhood(
    tenant_id: str,
    node_key: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_neo4j_graph_client),
) -> NeighborhoodResponse:
    """以这个实体为中心的邻域图（spec D6）。

    不存在的实体返回 404 而不是一张空图：空图看起来像「这个实体一条关系都
    没有」，而那是一句完全不同的话。

    两跳走哪些关系从 `tenant_relation_types` 读（本体结构页上勾了「支持链式
    查询」的那些），跟 `/agent/chat` 用的是同一份判据。写死一组的话，预览里
    能走两跳的关系和问答里能走两跳的关系会不一样——而用户会拿这两处互相
    印证：他在预览里看到 A 两跳能到 C，回去问却答不出来。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)

    try:
        center_term = await get_term_merged_by_node_key(
            review_conn, tenant_id=tenant_id, node_key=node_key
        )
    except TermNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"实体 {node_key} 不在这个租户的术语表里"
        ) from None

    confirmed_relation_type_defs = await list_relation_types(
        review_conn, tenant_id, status="confirmed"
    )
    chain_query_relation_types = {
        rt.relation_type for rt in confirmed_relation_type_defs if rt.allow_chain_query
    }

    try:
        rows = await graph_client.query_neighborhood(
            node_key, tenant_id=tenant_id,
            chain_query_relation_types=chain_query_relation_types,
        )
    except Exception:
        logger.warning("租户 %r 的实体 %r 邻域查询失败", tenant_id, node_key, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="图谱没查出来。这不代表这个实体没有关系——请稍后重试，一直失败请看服务端日志。",
        ) from None

    # 中心节点排第一，且**一定**保留：截断时把中心切掉的话，用户看到的是
    # 一张不含他要看的那个实体的图。它在结果行里未必出现（一条边都没有时
    # rows 是空的），所以从术语表里取。
    nodes: dict[str, GraphNode] = {
        center_term.node_key: GraphNode(
            node_key=center_term.node_key,
            standard_name=center_term.standard_name,
            term_type=center_term.term_type,
        )
    }
    edges: list[GraphEdge] = []
    for row in rows:
        for prefix in ("source", "target"):
            key = row[f"{prefix}_node_key"]
            if key not in nodes:
                nodes[key] = GraphNode(
                    node_key=key,
                    standard_name=row[f"{prefix}_name"],
                    term_type=row[f"{prefix}_type"],
                )
        edges.append(
            GraphEdge(
                source=row["source_node_key"],
                relation_type=row["relation_type"],
                target=row["target_node_key"],
            )
        )

    # 先数全量再截断。先截再数的话 total_nodes 恒等于上限，那句「其实有
    # 1013 个」就永远说不出来——而那正是这一页最容易退化成静默失败的地方。
    total_nodes = len(nodes)
    kept = list(nodes.values())[:GRAPH_PREVIEW_NODE_CAP]
    kept_keys = {n.node_key for n in kept}
    # 边也要跟着截。指向被截掉节点的边留着的话，sigma 找不到端点会抛异常，
    # 整张图变白——比少画几个节点糟得多。
    kept_edges = [e for e in edges if e.source in kept_keys and e.target in kept_keys]

    return NeighborhoodResponse(
        center=center_term.node_key,
        nodes=kept,
        edges=kept_edges,
        truncated=total_nodes > GRAPH_PREVIEW_NODE_CAP,
        total_nodes=total_nodes,
        shown_nodes=len(kept),
    )
