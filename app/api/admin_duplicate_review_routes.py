from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.duplicate_review_queue import (
    DuplicateReviewAlreadyResolvedError,
    DuplicateReviewNotFoundError,
    approve_duplicate_suggestion,
    count_pending_duplicate_suggestions,
    list_pending_duplicate_suggestions,
    reject_duplicate_suggestion,
)
from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant

router = APIRouter(
    prefix="/api/admin/duplicate-reviews", dependencies=[Depends(deps.require_admin_session)]
)


class DuplicateSuggestionListResponse(BaseModel):
    suggestions: list[dict]
    total: int


class ApproveDuplicateRequest(BaseModel):
    tenant_id: str
    keep_node_key: str


class RejectDuplicateRequest(BaseModel):
    tenant_id: str
    note: str | None = None


@router.get("", response_model=DuplicateSuggestionListResponse)
async def list_duplicate_suggestions(
    tenant_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> DuplicateSuggestionListResponse:
    offset = (page - 1) * page_size
    suggestions = await list_pending_duplicate_suggestions(
        review_conn, tenant_id=tenant_id, limit=page_size, offset=offset
    )
    total = await count_pending_duplicate_suggestions(review_conn, tenant_id=tenant_id)
    return DuplicateSuggestionListResponse(suggestions=suggestions, total=total)


@router.post("/{review_id}/approve")
async def approve(
    review_id: int,
    payload: ApproveDuplicateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    try:
        await require_active_tenant(review_conn, payload.tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        await approve_duplicate_suggestion(
            review_conn,
            review_id=review_id,
            tenant_id=payload.tenant_id,
            keep_node_key=payload.keep_node_key,
        )
    except DuplicateReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except DuplicateReviewAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"approved": True}


@router.post("/{review_id}/reject")
async def reject(
    review_id: int,
    payload: RejectDuplicateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    try:
        await require_active_tenant(review_conn, payload.tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    try:
        await reject_duplicate_suggestion(
            review_conn, review_id=review_id, tenant_id=payload.tenant_id, note=payload.note
        )
    except DuplicateReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except DuplicateReviewAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"rejected": True}
