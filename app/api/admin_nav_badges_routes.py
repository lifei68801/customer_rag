from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.graphrag.attribute_conflicts import count_conflicts
from app.graphrag.duplicate_review_queue import count_pending_duplicate_suggestions
from app.graphrag.review_queue import count_pending_reviews
from app.graphrag.terms_store import count_terms_merged

router = APIRouter(
    prefix="/api/admin/{tenant_id}/nav-badges",
    dependencies=[Depends(deps.require_admin_session)],
)


class NavBadgesResponse(BaseModel):
    pending_relations: int
    pending_duplicates: int
    #: 属性值冲突。跟上面两个一样是「有事等你」，不是规模。
    pending_conflicts: int
    #: 实体总数。它是规模不是待办——界面上刻意做得比待办徽标弱，因为
    #: 「有 20017 条实体」不是一件等着你处理的事。放同一个端点是因为它
    #: 和那三个待办数一起显示，不存在只要其中一个的场景。
    total_terms: int


@router.get("", response_model=NavBadgesResponse)
async def get_nav_badges(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> NavBadgesResponse:
    """侧边栏徽标的数字。

    四个数一次拿全（三个待办 + 一个规模）：它们总是一起显示，分开的话导航
    每次渲染都发好几
    个请求，而不存在只要其中一个的场景。

    没有待办返回 0，不是错误——那是最常见的状态。
    """
    return NavBadgesResponse(
        pending_relations=await count_pending_reviews(review_conn, tenant_id=tenant_id),
        pending_duplicates=await count_pending_duplicate_suggestions(
            review_conn, tenant_id=tenant_id
        ),
        pending_conflicts=await count_conflicts(review_conn, tenant_id=tenant_id),
        # 走合并视图：人工编辑层里的新增/删除也要算进去，否则这个数跟
        # 实体列表页自己显示的总数对不上。
        total_terms=await count_terms_merged(review_conn, tenant_id),
    )
