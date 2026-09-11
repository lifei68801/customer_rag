from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.api.admin_session import AdminSession
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import list_term_types

# 两个同名不同源的 UnknownCategoryError：terms_store 那个由 create_term 抛，
# ontology_constraints 那个由 add_allowed_combination 抛。直接 import 同名
# 符号会让后 import 的那个静默盖掉前一个——两条 except 里就有一条永远抓不到，
# 而那一条会变成 500。各自带上模块前缀，看得见它们不是一回事。
from app.graphrag.ontology_constraints import (
    UnknownCategoryError as ConstraintUnknownCategoryError,
)
from app.graphrag.ontology_constraints import (
    UnknownRelationTypeError,
    add_allowed_combination,
    list_allowed_combinations,
    to_combination_keys,
)
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
from app.graphrag.terms_store import (
    TermNameConflictError,
    create_term,
    list_terms_merged,
)
from app.graphrag.terms_store import UnknownCategoryError as TermUnknownCategoryError

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
    graph_client: Neo4jGraphClient = Depends(deps.get_neo4j_graph_client),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    return await _approve_with_names(
        tenant_id=tenant_id, review_id=review_id, review_conn=review_conn,
        graph_client=graph_client,
        subject_standard_name=payload.subject_standard_name,
        object_standard_name=payload.object_standard_name,
        subject_term_type=payload.subject_term_type,
        object_term_type=payload.object_term_type,
    )


async def _approve_with_names(
    *,
    tenant_id: str,
    review_id: int,
    review_conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    subject_standard_name: str,
    object_standard_name: str,
    subject_term_type: str | None,
    object_term_type: str | None,
) -> dict[str, bool]:
    """批准一条待审：写图 + 标记已处理。

    抽成函数是因为 create-missing-term 也要走同一条路。写两份的话，那一长串
    异常到状态码的映射（尤其是"图谱挂了返回 503 且记录留在队列里"这一支）
    会在两处分叉，而分叉出来的那一处正是最难发现的：它只在图谱挂掉时才走到。
    """
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
            subject_standard_name=subject_standard_name,
            object_standard_name=object_standard_name,
            tenant_id=tenant_id,
            graph_client=graph_client,
            terms=terms,
            now=datetime.now(),
            confirmed_relation_types=confirmed_relation_types,
            allowed_combinations=allowed_combinations,
            subject_term_type_hint=subject_term_type,
            object_term_type_hint=object_term_type,
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


class CreateMissingTermRequest(BaseModel):
    standard_name: str
    term_type: str
    side: Literal["subject", "object"]


@router.post("/{review_id}/create-missing-term")
async def create_missing_term(
    tenant_id: str,
    review_id: int,
    payload: CreateMissingTermRequest,
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_neo4j_graph_client),
) -> dict[str, bool]:
    """给「一端对不上」的那一条建出缺的实体，然后批准它。

    **两件事一起做。** 分成两步的话，审核员建完实体还得回来手动找到那条待审
    再批准，而中间任何中断都会留下「实体已建、审核还挂着」的状态——他下次
    看到这条会以为实体还没建，于是再建一次。

    顺序是先建实体、后批准。反过来不行：approve_review 要按标准名去术语表里
    查这一端，实体还没建的话它查不到。

    写图失败时这条审核**保持 pending**：approve_review 先写图、后改状态
    （见它的写入顺序），图那一步抛异常时 UPDATE 根本没执行。实体已经建出来
    了，重试时会撞上重名——那一支下面单独处理成"就用已有的那个"，因为用户
    这次要做的事跟上次完全一样。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)

    # 类型必须是本体里已有的。放行的话，审核这个动作自己就制造出了一个孤儿
    # 类型——而它绕过了本体那一层的全部校验，之后没有任何东西能匹配上它。
    #
    # 查 **confirmed**：create_term 内部的 validate_term_categories 查的就是
    # confirmed（terms_store.py:795）。这里用别的口径的话会出现"这一步放行了、
    # 下一行 create_term 却抛 UnknownCategoryError"——用户拿到的是 500，而
    # 他填的东西其实只是还没确认。
    known_types = {
        c.value for c in await list_term_types(review_conn, tenant_id, status="confirmed")
    }
    if payload.term_type not in known_types:
        raise HTTPException(
            status_code=400,
            detail=(
                f"类型 {payload.term_type!r} 不在这个租户的本体里。"
                f"先去本体结构页把它建出来，或者从已有的这些里挑一个："
                f"{'、'.join(sorted(known_types)) or '（一个都还没有）'}"
            ),
        )

    reviews = await list_pending_reviews(review_conn, tenant_id=tenant_id)
    review = next((r for r in reviews if r["review_id"] == review_id), None)
    if review is None:
        raise HTTPException(status_code=404, detail="待审核记录不存在，或者已经处理过了")

    try:
        await create_term(
            review_conn, tenant_id=tenant_id, standard_name=payload.standard_name,
            aliases=[], term_type=payload.term_type, source="review",
        )
    except TermNameConflictError:
        # 已经有同名的了。多数情况下这是"上一次建成功了、批准那步没成"
        # （见 docstring 里的顺序说明），直接往下走就对了。
        #
        # 但有一种情况不能往下走：那条同名记录**在合并视图里是被人工删除的**
        # （term_edits 里的 __deleted__）。_check_name_conflict 查的是 terms
        # 裸表，看得见它；而下面的 _approve_with_names 查的是合并视图，看不见
        # ——于是用户会连着收到两句自相矛盾的话（"已经有了" 然后 "不在术语表
        # 里"），而且从这个界面无论如何都走不出去。说清楚，并给出两条路。
        merged = await list_terms_merged(review_conn, tenant_id)
        if payload.standard_name not in {t.standard_name for t in merged}:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{payload.standard_name!r} 这个名字被一条**已人工删除**的实体占着，"
                    f"新建不了、也批准不了。去实体明细页把那一条恢复，"
                    f"或者换一个名字。"
                ),
            ) from None
    except TermUnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # 缺的那一端用新建的名字，另一端用管线给的候选名。
    subject_name = (
        payload.standard_name if payload.side == "subject" else review["subject_candidate"]
    )
    object_name = (
        payload.standard_name if payload.side == "object" else review["object_candidate"]
    )
    return await _approve_with_names(
        tenant_id=tenant_id, review_id=review_id, review_conn=review_conn,
        graph_client=graph_client, subject_standard_name=subject_name,
        object_standard_name=object_name,
        subject_term_type=payload.term_type if payload.side == "subject" else None,
        object_term_type=payload.term_type if payload.side == "object" else None,
    )


@router.post("/{review_id}/allow-combination")
async def allow_combination(
    tenant_id: str,
    review_id: int,
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    """把这条审核的类型组合加进本体**草稿**的白名单。

    **加完不批准，也不确认整份草稿。** 两条都是有意的：

    - `add_allowed_combination` 写的是 `status='draft'`
      （ontology_constraints.py:137），而 `approve_review` 查的是 confirmed
      的组合。加完立刻批准必然撞 `RelationNotInConfirmedOntologyError`。
    - 唯一能一步到位的做法是顺手 `confirm_ontology`，而它把**整份草稿**原地
      提升为已确认（ontology_lifecycle.py:249-253）——别人正在编辑中的半成品
      本体会被一次审核操作悄悄发布出去。

    所以回包里带上 next_step 说清楚下一步。只回 `{"ok": true}` 的话，审核员
    会以为这条处理完了，而它还挂在队列里。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)

    reviews = await list_pending_reviews(review_conn, tenant_id=tenant_id)
    review = next((r for r in reviews if r["review_id"] == review_id), None)
    if review is None:
        raise HTTPException(status_code=404, detail="待审核记录不存在，或者已经处理过了")

    subject_type = review["subject_type_candidate"]
    object_type = review["object_type_candidate"]
    if not subject_type or not object_type:
        # 加进去的会是一个带空类型的组合，它匹配不上任何东西——白名单里多了
        # 一条永远不生效的规则，而用户以为自己已经放宽了本体。
        raise HTTPException(
            status_code=400,
            detail=(
                "这条待审没有识别出两端的类型，没法加白名单。"
                "先在「一端对不上」那一页把缺的实体建出来（建的时候要选类型），"
                "或者直接去本体结构页手工加这条组合。"
            ),
        )

    try:
        await add_allowed_combination(
            review_conn, tenant_id,
            subject_term_type=subject_type,
            relation_type=review["relation_type"],
            object_term_type=object_type,
            actor=session.username,
        )
    except (ConstraintUnknownCategoryError, UnknownRelationTypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return {
        "combination": f"{subject_type} -{review['relation_type']}-> {object_type}",
        "next_step": (
            "已加进本体草稿。去「本体结构」页确认这份草稿之后，回来批准这条待审"
            "——在那之前它还在队列里。"
        ),
    }


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
