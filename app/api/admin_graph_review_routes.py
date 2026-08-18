from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    StandardNameNotInTermsError,
    approve_review,
    count_pending_reviews,
    count_resolved_reviews,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)
from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant
from app.graphrag.terms_store import list_terms

router = APIRouter(
    prefix="/api/admin/graph-reviews", dependencies=[Depends(deps.require_admin_session)]
)


class ReviewListResponse(BaseModel):
    reviews: list[dict]
    total: int


class ApproveRequest(BaseModel):
    tenant_id: str
    subject_standard_name: str
    object_standard_name: str


class RejectRequest(BaseModel):
    tenant_id: str
    note: str | None = None


@router.get("", response_model=ReviewListResponse)
async def list_reviews(
    tenant_id: str,
    status: str = "pending",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> ReviewListResponse:
    offset = (page - 1) * page_size
    if status == "pending":
        reviews = await list_pending_reviews(
            review_conn, tenant_id=tenant_id, limit=page_size, offset=offset
        )
        total = await count_pending_reviews(review_conn, tenant_id=tenant_id)
    elif status in ("approved", "rejected"):
        reviews = await list_resolved_reviews(
            review_conn, tenant_id=tenant_id, status=status, limit=page_size, offset=offset
        )
        total = await count_resolved_reviews(review_conn, tenant_id=tenant_id, status=status)
    elif status == "all":
        # status=None 让 list_resolved_reviews/count_resolved_reviews 同时
        # 统计 approved+rejected；路由层用 "all" 这个显式值表达"不筛选"，
        # 不直接暴露 None 给客户端。
        reviews = await list_resolved_reviews(
            review_conn, tenant_id=tenant_id, status=None, limit=page_size, offset=offset
        )
        total = await count_resolved_reviews(review_conn, tenant_id=tenant_id, status=None)
    else:
        raise HTTPException(status_code=400, detail="status 必须是 pending/approved/rejected/all")
    return ReviewListResponse(reviews=reviews, total=total)


@router.post("/{review_id}/approve")
async def approve(
    review_id: int,
    payload: ApproveRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    try:
        await require_active_tenant(review_conn, payload.tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    # 这个路由自己的权威 tenant_id 是 payload.tenant_id，不用 deps.get_terms
    # 那套独立的 gateway_tenant_id 解析——两者在这条请求里可能不是同一个
    # 值，直接按 payload.tenant_id 加载术语表，避免跨租户读到错的术语表。
    terms: list[Term] = await list_terms(review_conn, payload.tenant_id)
    try:
        await approve_review(
            review_conn,
            review_id=review_id,
            subject_standard_name=payload.subject_standard_name,
            object_standard_name=payload.object_standard_name,
            tenant_id=payload.tenant_id,
            graph_client=graph_client,
            terms=terms,
            now=datetime.now(),
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    except StandardNameNotInTermsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"approved": True}


@router.post("/{review_id}/reject")
async def reject(
    review_id: int,
    payload: RejectRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    try:
        await require_active_tenant(review_conn, payload.tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        await reject_review(
            review_conn, review_id=review_id, tenant_id=payload.tenant_id, note=payload.note
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    return {"rejected": True}
