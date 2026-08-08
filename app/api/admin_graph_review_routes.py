from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    approve_review,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)

router = APIRouter(
    prefix="/admin/graph-reviews", dependencies=[Depends(deps.require_admin_session)]
)


class ReviewListResponse(BaseModel):
    reviews: list[dict]


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
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> ReviewListResponse:
    if status == "pending":
        reviews = await list_pending_reviews(review_conn, tenant_id=tenant_id)
    elif status in ("approved", "rejected"):
        reviews = await list_resolved_reviews(review_conn, tenant_id=tenant_id, status=status)
    else:
        raise HTTPException(status_code=400, detail="status 必须是 pending/approved/rejected")
    return ReviewListResponse(reviews=reviews)


@router.post("/{review_id}/approve")
async def approve(
    review_id: int,
    payload: ApproveRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    try:
        await approve_review(
            review_conn,
            review_id=review_id,
            subject_standard_name=payload.subject_standard_name,
            object_standard_name=payload.object_standard_name,
            tenant_id=payload.tenant_id,
            graph_client=graph_client,
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    return {"approved": True}


@router.post("/{review_id}/reject")
async def reject(
    review_id: int,
    payload: RejectRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    try:
        await reject_review(
            review_conn, review_id=review_id, tenant_id=payload.tenant_id, note=payload.note
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    return {"rejected": True}
