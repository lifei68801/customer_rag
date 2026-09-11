from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.neo4j_client import GraphWriteProtocol
from app.graphrag.organizations_store import list_tenants_with_organization
from app.graphrag.tenant_stats import collect_tenant_stats

logger = logging.getLogger(__name__)


class Domain(BaseModel):
    tenant_id: str
    name: str
    #: 没挂组织的租户这两项为 None。那是合法状态（存量租户），不是错误。
    org_id: str | None
    org_name: str | None


class DomainListResponse(BaseModel):
    domains: list[Domain]


class DomainStats(BaseModel):
    tenant_id: str
    term_count: int
    edge_count: int
    document_count: int
    pending_review_count: int
    #: 表格/数据库导入进来的实体行数（spec §5 卡片第四格）。
    sheet_row_count: int
    #: 失效的手写引导问题数（spec 前台硬规矩之二）。>0 时卡片上出一条待办。
    stale_question_count: int


# 领域清单。**非租户**路径：「我能看到哪些领域」对每个角色都要回答得出来，
# 而且看板是登录后的落地页，那时还没有"当前租户"。安全性完全靠内容按
# list_accessible_tenant_ids 过滤，跟 GET /api/admin/personas 同一个模式。
router = APIRouter(
    prefix="/api/admin/dashboard",
    dependencies=[Depends(deps.require_admin_session)],
)

# 逐领域统计。**租户内**路径，挂进 app/main.py 的 tenant_scoped，那里统一挂了
# require_tenant_access。
#
# 路径形状是 /api/admin/{tenant_id}/dashboard/stats 而不是
# /api/admin/dashboard/domains/{tenant_id}/stats：后者过不了
# tests/api/test_admin_route_shapes.py 的归类守卫——它只认租户段在第三段的
# 形状，中间带租户段的路径会被归成非租户路由，于是守卫会**要求它不挂**
# require_tenant_access，跟这个端点的安全要求正面矛盾。
stats_router = APIRouter(
    prefix="/api/admin/{tenant_id}/dashboard",
    dependencies=[Depends(deps.require_admin_session)],
)


@router.get("/domains", response_model=DomainListResponse)
async def list_domains(
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> DomainListResponse:
    """这个账号能看到的领域清单。**不含任何统计数字。**

    带上数字的话这个端点就得等所有领域都算完才返回，而每个领域的边计数都是
    一次图查询——第一张卡也要等最慢的那个领域（spec D5 裁决二）。前端拿到
    清单先把卡片轮廓画出来，每张卡再各自请求自己的统计、各自落位。

    按 accessible 过滤：聚合数字也是信息，给 member 看到他无权访问的领域
    有多少实体等于泄露业务规模。这里连领域的存在本身都不给。
    """
    accessible = await deps.list_accessible_tenant_ids(review_conn, session)
    rows = await list_tenants_with_organization(review_conn)
    return DomainListResponse(
        domains=[
            Domain(
                tenant_id=row["tenant_id"],
                name=row["name"],
                org_id=row["org_id"],
                org_name=row["org_name"],
            )
            for row in rows
            if accessible is None or row["tenant_id"] in accessible
        ]
    )


@stats_router.get("/stats", response_model=DomainStats)
async def get_domain_stats(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_neo4j_graph_client),
) -> DomainStats:
    """一个领域的四个数字。全部实时算（spec D5）。

    图谱查不通时返回 503，**不返回一组带 0 的数字**。返回 0 的话用户看到的是
    「这个领域一条关系都没有」——一个看起来正常、实际是错的数字，他会据此
    以为数据没导进去然后去重跑一遍 ETL。前端据这个状态码把这一张卡渲染成
    「统计失败」+ 重试，其余卡不受影响。

    回包里一个数字都不给（连 term_count 也不给）：给了就等于回答了「有多少」，
    而这次请求恰恰没能回答出来。
    """
    try:
        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, graph_client, tenant_id=tenant_id
        )
    except Exception:
        # 不点名原因。这里接的是 collect_tenant_stats 抛出来的任何东西——
        # 图谱连不上是最常见的一种，但少一张表、SQL 出错、租户不存在同样
        # 会落到这里。写死"图谱可能连不上"就是在断言一件没验证过的事，
        # 看到的人会去查一个好好的 Neo4j。真正的原因带 exc_info 进日志。
        logger.warning("租户 %r 的看板统计失败", tenant_id, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="统计没算出来。这不代表这个领域是空的——请稍后重试，一直失败请看服务端日志。",
        ) from None
    return DomainStats(
        tenant_id=stats.tenant_id,
        term_count=stats.term_count,
        edge_count=stats.edge_count,
        document_count=stats.document_count,
        pending_review_count=stats.pending_review_count,
        sheet_row_count=stats.sheet_row_count,
        stale_question_count=stats.stale_question_count,
    )
