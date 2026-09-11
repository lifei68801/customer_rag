from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.neo4j_client import Neo4jGraphClient

logger = logging.getLogger(__name__)

#: 一次最多列多少条。
#:
#: 上限本身不重要，「超了要说出来」才重要——默默少列的话，运维会以为脏边
#: 只有这么多，清完就以为干净了。
DEFAULT_LIMIT = 500

router = APIRouter(
    prefix="/api/admin/{tenant_id}/dirty-edges",
    dependencies=[Depends(deps.require_admin_session)],
)


class DirtyEdgesResponse(BaseModel):
    edges: list[dict]
    #: 结果是不是被上限截断了。**不是**"还剩多少条"——那需要再跑一次
    #: count，而这一页的用途是"让人看见并开始清理"，不是给一个精确总数。
    truncated: bool
    limit: int


@router.get("", response_model=DirtyEdgesResponse)
async def list_dirty_edges(
    tenant_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=2000),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_neo4j_graph_client),
) -> DirtyEdgesResponse:
    """整个租户的脏边。

    **这一页存在的理由就是"不锚定某个实体"**：脏边的列举此前只在实体详情页
    里，你得先知道是哪个实体才看得到它的脏边——而脏边的特点恰恰是没人知道
    它们在哪。运维只能一个实体一个实体点过去。

    图谱查不通时返回 503 而不是空列表。空列表读起来是「一条脏边都没有」，
    那是这一页最不该说错的一句话——运维会据此认为数据是干净的。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        edges, truncated = await graph_client.list_tenant_dirty_edges(
            tenant_id=tenant_id, limit=limit
        )
    except Exception:
        logger.warning("租户 %r 的脏边清单查询失败", tenant_id, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="脏边清单没查出来。这不代表没有脏边——请稍后重试，一直失败请看服务端日志。",
        ) from None
    return DirtyEdgesResponse(edges=edges, truncated=truncated, limit=limit)
