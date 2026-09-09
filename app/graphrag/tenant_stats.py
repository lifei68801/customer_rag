from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import aiosqlite

from app.graphrag.duplicate_review_queue import count_pending_duplicate_suggestions
from app.graphrag.review_queue import count_pending_reviews
from app.graphrag.terms_store import count_terms_merged
from app.ingestion.tracking import count_tracked_files


@dataclass(frozen=True)
class TenantStats:
    """一个领域在看板上的四个数字。"""

    tenant_id: str
    term_count: int
    edge_count: int
    document_count: int
    pending_review_count: int


class EdgeCounter(Protocol):
    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int: ...


async def collect_tenant_stats(
    review_conn: aiosqlite.Connection,
    ingestion_conn: aiosqlite.Connection,
    graph_client: EdgeCounter,
    *,
    tenant_id: str,
) -> TenantStats:
    """一个领域的统计。全部实时算（spec D5）。

    四个数字来自四个不同的地方，所以这是个跨模块的聚合器：术语表、图谱、
    文档追踪表、审核队列。每一处都复用各自模块里已有的计数函数，不在这里
    另写 SQL——另写一份的代价是口径将来会分叉，而看板上的数字跟对应页面
    对不上正是最难查的一类问题。

    **图谱查不通时不接异常，让它抛出去。** 报 0 的话用户看到的是「这个领域
    一条关系都没有」——一个看起来正常、实际是错的数字，他会据此以为数据没
    导进去然后去重跑一遍 ETL。调用方接住它，把这张卡渲染成「统计失败」，
    用户看得见也够得着纠正。

    实体数走 `count_terms_merged`（合并视图）而不是 `count_terms`（裸表）：
    人工删除（`__deleted__`）的行仍在 terms 里但不出现在实体明细页的列表中。
    看板上的数字必须等于用户在那一页数得出来的条数。
    """
    term_count = await count_terms_merged(review_conn, tenant_id)
    edge_count = await graph_client.count_relation_edges_for_tenant(tenant_id=tenant_id)
    document_count = await count_tracked_files(ingestion_conn, tenant_id=tenant_id)
    # 两个审核队列都要数。只数关系队列的话，一个「0 条待审关系 + 12 条疑似
    # 重复」的领域在看板上显示「待审 0」且不给入口，而侧边栏同时显示
    # 「数据审核：12」——同一个人同一屏看到两个互相矛盾的数字，他会以为
    # 其中一个坏了。侧边栏的口径见 app/api/admin_nav_badges_routes.py。
    pending_review_count = await count_pending_reviews(
        review_conn, tenant_id=tenant_id
    ) + await count_pending_duplicate_suggestions(review_conn, tenant_id=tenant_id)
    return TenantStats(
        tenant_id=tenant_id,
        term_count=term_count,
        edge_count=edge_count,
        document_count=document_count,
        pending_review_count=pending_review_count,
    )
