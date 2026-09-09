from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.attribute_conflicts import (
    ConflictAlreadyResolvedError,
    ConflictNotFoundError,
    count_conflicts,
    list_conflicts,
    resolve_conflict,
)
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.terms_store import (
    TermNotFoundError,
    get_term_merged_by_node_key,
    set_extra_property,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/{tenant_id}/conflicts",
    dependencies=[Depends(deps.require_admin_session)],
)


class ConflictListResponse(BaseModel):
    conflicts: list[dict]
    total: int


class ResolveRequest(BaseModel):
    value: str


@router.get("", response_model=ConflictListResponse)
async def list_attribute_conflicts(
    tenant_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> ConflictListResponse:
    """待处理的属性值冲突。

    只列 pending：已决议的混在里面的话，审核员会重复处理——他看不出哪些
    已经定过了。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    conflicts = await list_conflicts(
        review_conn, tenant_id=tenant_id, limit=page_size, offset=(page - 1) * page_size
    )
    return ConflictListResponse(
        conflicts=conflicts, total=await count_conflicts(review_conn, tenant_id=tenant_id)
    )


@router.post("/{conflict_id}/resolve")
async def resolve_attribute_conflict(
    tenant_id: str,
    conflict_id: int,
    payload: ResolveRequest,
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, str]:
    """定下这条冲突用哪个值：写回 terms、同步图谱、标记已决议。

    **三件事的顺序是有讲究的**，最后一步必须是标记已决议：

    1. 写回 terms。不写回的话，审核员选完发现实体上的值没变，而系统说
       「已解决」。
    2. 同步图谱。只改 SQLite 的话，问答拿到的还是旧值——而审核页显示这条
       已经处理完了。
    3. 标记已决议。放在最后，是因为前两步任何一步失败时这条必须还在队列里：
       标了的话它从队列消失，而数据还是旧的，没有任何地方能再发现它。

    代价是前两步成功、第三步之前进程挂掉时，terms 已改而冲突仍待处理——
    审核员会再看到它一次，再选一次同样的值。重复一次无害（写回是幂等的），
    而反过来（标了但没生效）是不可发现的。

    **并发保护只覆盖冲突表那一行**：`resolve_conflict` 的 UPDATE 带
    `status='pending'`，所以两个人同时决议时只有一个能把它标成已处理。但前
    两步没有这道闸——两个人各自选了不同的值时，两次写回和两次图谱同步都会
    执行，最后落地的是**后完成的那个**，而只有先完成的那个人会看到成功。
    今天没修：审核是低频人工操作，两个人同时决议同一条属性冲突这件事没有
    在真实使用里出现过；真要修得给 terms 那一步也加一道版本判据。记在这里，
    别让它被读成"这里已经并发安全了"。

    `value` 不限于那两个值之一：两个来源都错是可能的（比如两张表都漏了
    单位），强制二选一等于逼审核员选一个已知是错的。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)

    rows = await list_conflicts(review_conn, tenant_id=tenant_id)
    conflict = next((c for c in rows if c["conflict_id"] == conflict_id), None)
    if conflict is None:
        raise HTTPException(status_code=404, detail="这条冲突不存在，或者已经处理过了")

    node_key = conflict["node_key"]
    try:
        await set_extra_property(
            review_conn, tenant_id=tenant_id, node_key=node_key,
            field=conflict["field"], value=payload.value,
            # 来源写成一句人话。不写的话那一列会留着原来那次导入的来源，
            # 下一次冲突会显示「用 42（来自 商品表.xlsx）」——而商品表说的
            # 是 39，它从没说过 42。
            value_source=f"人工决议：{session.username}",
        )
    except ValueError as exc:
        # 填了个这个字段的声明类型不认的值（比如 number 字段填了"四十五"）。
        # 400 而不是 500：这是"改输入才行"，报错原文里点名了是哪个字段、
        # 声明的是什么类型、填的是什么。
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except TermNotFoundError:
        # 实体在这条冲突记下来之后被删了。冲突留在队列里没意义——它指向一个
        # 不存在的东西，审核员选什么都写不进去。说清楚，让他去驳回或忽略。
        raise HTTPException(
            status_code=409,
            detail=f"实体 {node_key} 已经不在术语表里了（可能被删或被合并），这条冲突无处可写。",
        ) from None

    try:
        merged = await get_term_merged_by_node_key(review_conn, tenant_id=tenant_id, node_key=node_key)
        await graph_client.sync_term(merged)
    except Exception:
        logger.exception(
            "冲突 %s（租户 %r）写回 terms 成功但同步图谱失败——这条仍在待处理队列里，可重试",
            conflict_id, tenant_id,
        )
        raise HTTPException(
            status_code=503,
            detail="值已写进术语表，但同步图谱失败。这条冲突仍在队列里，请稍后重试。",
        ) from None

    try:
        await resolve_conflict(
            review_conn, tenant_id=tenant_id, conflict_id=conflict_id,
            chosen_value=payload.value, resolved_by=session.username,
        )
    except ConflictNotFoundError:
        raise HTTPException(status_code=404, detail="这条冲突不存在") from None
    except ConflictAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="这条冲突已经有人处理过了") from None

    return {"resolved_value": payload.value}
