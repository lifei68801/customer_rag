from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.duplicate_review_queue import (
    DuplicateReviewAlreadyResolvedError,
    DuplicateReviewNotFoundError,
    approve_duplicate_suggestion,
    count_pending_duplicate_suggestions,
    list_pending_duplicate_suggestions,
    reject_duplicate_suggestion,
)
from app.graphrag.terms_store import (
    InvalidExtraPropertyTypeError,
    TermNameConflictError,
    UnknownCategoryError,
)

router = APIRouter(
    prefix="/api/admin/{tenant_id}/duplicate-reviews",
    dependencies=[Depends(deps.require_admin_session)],
)


class DuplicateSuggestionListResponse(BaseModel):
    suggestions: list[dict]
    total: int


class ApproveDuplicateRequest(BaseModel):
    keep_node_key: str


class RejectDuplicateRequest(BaseModel):
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
    tenant_id: str,
    review_id: int,
    payload: ApproveDuplicateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await approve_duplicate_suggestion(
            review_conn,
            review_id=review_id,
            tenant_id=tenant_id,
            keep_node_key=payload.keep_node_key,
        )
    except DuplicateReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except DuplicateReviewAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # approve_duplicate_suggestion() 内部经 terms_store.update_term() 做真正
    # 的合并写入，可能抛出 TermNameConflictError/UnknownCategoryError/
    # InvalidExtraPropertyTypeError——这三个都不是 ValueError 的子类，之前
    # 只兜底 ValueError 会让真实的合并冲突（例如 ETL 创建的术语绕开了通常的
    # 冲突检查）在这里变成不透明的 500，违反设计规格里"合并操作在这种情况
    # 下应该失败并让审核人员看到明确的错误信息，不能静默失败"的要求（见
    # docs/superpowers/specs/2026-08-26-duplicate-term-detection-design.md
    # 第 102 行）。状态码映射跟着这个路由自己已有的语义走（不是照抄
    # admin_terms_routes.py——那边的 TermNameConflictError 映射的是 400，
    # 因为那是"新建/编辑单条术语时提交的名字本身有问题"；这里是"审批一个
    # 合并请求时发现目标状态已经被占用"，跟同一路由里
    # DuplicateReviewAlreadyResolvedError 是同一种"资源冲突"语义，映射
    # 409）：分类不存在/属性类型不合法这两种，语义上是"请求本身有问题"，
    # 映射 400。
    except TermNameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (UnknownCategoryError, InvalidExtraPropertyTypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"approved": True}


@router.post("/{review_id}/reject")
async def reject(
    tenant_id: str,
    review_id: int,
    payload: RejectDuplicateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await reject_duplicate_suggestion(
            review_conn, review_id=review_id, tenant_id=tenant_id, note=payload.note
        )
    except DuplicateReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except DuplicateReviewAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"rejected": True}
