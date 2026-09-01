from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.graphrag.duplicate_review_queue import count_pending_duplicate_suggestions
from app.graphrag.review_queue import count_pending_reviews

router = APIRouter(
    prefix="/api/admin/nav-badges", dependencies=[Depends(deps.require_admin_session)]
)


class NavBadgesResponse(BaseModel):
    pending_relations: int
    pending_duplicates: int


@router.get("", response_model=NavBadgesResponse)
async def get_nav_badges(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> NavBadgesResponse:
    """侧边栏徽标的数字。

    两个数一次拿全：它们总是一起显示，分两个端点的话导航每次渲染都发两
    个请求，而不存在只要其中一个的场景。

    没有待办返回 0，不是错误——那是最常见的状态。
    """
    return NavBadgesResponse(
        pending_relations=await count_pending_reviews(review_conn, tenant_id=tenant_id),
        pending_duplicates=await count_pending_duplicate_suggestions(
            review_conn, tenant_id=tenant_id
        ),
    )
