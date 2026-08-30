from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.review_queue import (
    RelationNotInConfirmedOntologyError,
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
    subject_term_type: str | None = None
    object_term_type: str | None = None


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
    await require_active_tenant_or_404(review_conn, payload.tenant_id)
    # 这个路由自己的权威 tenant_id 是 payload.tenant_id，不用 deps.get_terms
    # 那套独立的 gateway_tenant_id 解析——两者在这条请求里可能不是同一个
    # 值，直接按 payload.tenant_id 加载术语表，避免跨租户读到错的术语表。
    terms: list[Term] = await list_terms(review_conn, payload.tenant_id)
    # 与 normalize_and_write_relations() 的自动写入路径共用同一套"已确认
    # 本体范围"数据源：这里查的是 status="confirmed"，不是草稿——审核员
    # 批准动作最终写图谱，必须过跟自动路径一样的闸门，见
    # RelationNotInConfirmedOntologyError 的说明。
    confirmed_relation_types = {
        rt.relation_type
        for rt in await list_relation_types(review_conn, payload.tenant_id, status="confirmed")
    }
    allowed_combinations = {
        (c.subject_term_type, c.relation_type, c.object_term_type)
        for c in await list_allowed_combinations(review_conn, payload.tenant_id, status="confirmed")
    }
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
            confirmed_relation_types=confirmed_relation_types,
            allowed_combinations=allowed_combinations,
            subject_term_type_hint=payload.subject_term_type,
            object_term_type_hint=payload.object_term_type,
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    except StandardNameNotInTermsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RelationNotInConfirmedOntologyError as exc:
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
    await require_active_tenant_or_404(review_conn, payload.tenant_id)
    try:
        await reject_review(
            review_conn, review_id=review_id, tenant_id=payload.tenant_id, note=payload.note
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    return {"rejected": True}
