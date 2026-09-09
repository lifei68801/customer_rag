from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_constraints import list_allowed_combinations, to_combination_keys
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.review_queue import (
    RelationNotInConfirmedOntologyError,
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    StandardNameNotInTermsError,
    approve_review,
    count_pending_by_reason,
    count_pending_reviews,
    count_resolved_reviews,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)
from app.graphrag.terms_store import list_terms_merged

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/{tenant_id}/graph-reviews",
    dependencies=[Depends(deps.require_admin_session)],
)


#: 分页 → 它收哪几种 reason。
#:
#: **映射写在后端而不是前端。** 写在前端的话，reason 字符串会同时存在于前后端
#: 两处，改一个忘一个就是一个永远空着的分页——而它看起来完全正常，没有任何
#: 报错，队列里的东西就那么积着。
#:
#: 拆成四页不是为了好看：审核员面对这四类要做的事完全不同——fuzzy 是确认一个
#: 候选，unresolved 是给那一端建个实体，out_of_ontology 是决定要不要放宽本体，
#: bad_type 是改关系类型。此前它们共用同一对「批准/驳回」按钮，混在一屏里，
#: 每条都得先判断"这条属于哪一类"。
#:
#: 取值来自 app/graphrag/normalization.py（119/186/202/232/279）。
#: tests/api/test_admin_graph_review_routes.py::
#: test_every_reason_the_pipeline_writes_lands_in_exactly_one_tab 从那份源码里
#: 数出真实写入的 reason 跟这里比对——将来加一种新 reason 却忘了归页时，
#: 那条用例会红，而不是让那批待办永远不出现在任何页面上。
TAB_REASONS: dict[str, list[str]] = {
    "fuzzy": ["fuzzy_match_needs_confirmation"],
    "unresolved": ["subject_unresolved", "object_unresolved"],
    "out_of_ontology": ["not_in_confirmed_ontology"],
    "bad_type": ["invalid_relation_type"],
}


class ReviewCountsResponse(BaseModel):
    fuzzy: int
    unresolved: int
    out_of_ontology: int
    bad_type: int


class ReviewListResponse(BaseModel):
    reviews: list[dict]
    total: int


class ApproveRequest(BaseModel):
    subject_standard_name: str
    object_standard_name: str
    subject_term_type: str | None = None
    object_term_type: str | None = None


class RejectRequest(BaseModel):
    note: str | None = None


@router.get("", response_model=ReviewListResponse)
async def list_reviews(
    tenant_id: str,
    status: str = "pending",
    tab: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> ReviewListResponse:
    """tab 只对 status=pending 有意义：四个分页分的是**待审**的理由，
    已处理的记录按时间倒序看历史，不按理由分。

    tab 拼错返回 400 而不是空列表：返回空的话，前端拼错一个字母就得到一个
    永远空着的分页，而「这一类没有待办」和「这个请求根本没问对」在界面上
    长得一模一样。
    """
    offset = (page - 1) * page_size
    if tab is not None and tab not in TAB_REASONS:
        raise HTTPException(
            status_code=400,
            detail=f"tab 必须是 {'/'.join(TAB_REASONS)} 之一，收到的是 {tab!r}",
        )
    reasons = TAB_REASONS[tab] if tab is not None else None
    if status == "pending":
        reviews = await list_pending_reviews(
            review_conn, tenant_id=tenant_id, limit=page_size, offset=offset, reasons=reasons
        )
        # total 跟着 tab 走。不跟的话分页器按全部条数算，用户翻到第二页看到
        # 的是空的，而他以为那里还有东西。
        total = await count_pending_reviews(review_conn, tenant_id=tenant_id, reasons=reasons)
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


@router.get("/counts", response_model=ReviewCountsResponse)
async def get_review_counts(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> ReviewCountsResponse:
    """四个分页各有几条待审。

    每一页都给一个数，空的那页是 0 而不是缺这个 key：缺 key 的话前端得写
    `?? 0` 兜底，而那会把「后端没算这一页」和「这一页真的是 0」混成一件事。
    """
    by_reason = await count_pending_by_reason(review_conn, tenant_id=tenant_id)
    return ReviewCountsResponse(
        **{tab: sum(by_reason.get(r, 0) for r in reasons) for tab, reasons in TAB_REASONS.items()}
    )


@router.post("/{review_id}/approve")
async def approve(
    tenant_id: str,
    review_id: int,
    payload: ApproveRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    # 这个路由的权威 tenant_id 是路径里的这个，不走 deps.get_terms 那套
    # 独立的 gateway_tenant_id 解析——两者在这条请求里可能不是同一个值，
    # 直接按路径参数加载术语表，避免跨租户读到错的术语表。
    terms: list[Term] = await list_terms_merged(review_conn, tenant_id)
    # 与 normalize_and_write_relations() 的自动写入路径共用同一套"已确认
    # 本体范围"数据源：这里查的是 status="confirmed"，不是草稿——审核员
    # 批准动作最终写图谱，必须过跟自动路径一样的闸门，见
    # RelationNotInConfirmedOntologyError 的说明。
    confirmed_relation_types = {
        rt.relation_type
        for rt in await list_relation_types(review_conn, tenant_id, status="confirmed")
    }
    allowed_combinations = to_combination_keys(
        await list_allowed_combinations(review_conn, tenant_id, status="confirmed")
    )
    try:
        await approve_review(
            review_conn,
            review_id=review_id,
            subject_standard_name=payload.subject_standard_name,
            object_standard_name=payload.object_standard_name,
            tenant_id=tenant_id,
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
    except HTTPException:
        # 上面那几个 except 转出来的 HTTPException 不该被下面的兜底再吞一次。
        raise
    except Exception:
        # 兜底：Neo4j 连接失败、超时这类基础设施异常。上面六个 except 覆盖的
        # 都是"输入有问题、你得改"的业务异常，基础设施异常不在其中，此前会
        # 变成不透明的 500——审核员看不出是自己填错了还是图谱挂了，而这两种
        # 的应对完全不同。
        #
        # 返回 503 而不是 500 是有意的：语义是"服务暂时不可用"，明确告诉调用
        # 方这是**可重试**的，跟 400 那类"改输入才行"区分开。批量批准时这一点
        # 尤其重要——图谱挂掉会让 10 条连续失败，用户需要知道该等一等再整批
        # 重试，而不是逐条去检查自己填了什么。
        #
        # 记录仍然停在 pending：approve_review 先写图谱、后改状态，图谱这一步
        # 抛异常时那条 UPDATE 根本没执行（见 review_queue.approve_review 的
        # 写入顺序）。merge_relation 是 MERGE、幂等，重试安全。
        logger.exception(
            "批准候选 %s（租户 %r）时图谱写入失败——记录仍在待审队列，可重试",
            review_id, tenant_id,
        )
        raise HTTPException(
            status_code=503,
            detail="图谱写入失败，该记录仍在待审队列中，请稍后重试。",
        )
    return {"approved": True}


@router.post("/{review_id}/reject")
async def reject(
    tenant_id: str,
    review_id: int,
    payload: RejectRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await reject_review(
            review_conn, review_id=review_id, tenant_id=tenant_id, note=payload.note
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    return {"rejected": True}
