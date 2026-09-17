"""智能创建（访谈式建模）的接口。只做编排：读会话、校验乐观锁、调
app/graphrag/ontology_interview.py 的纯逻辑、存回、翻译异常。

不新增写草稿的端点：前端审完骨架后走既有的 apply-preview + draft/replace，
理由同建模工作台（admin_modeling_workspace_routes.py 文件头）。

prefix 沿用 /api/admin/ontology：路由形状测试的租户前缀白名单已经覆盖它。
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api import deps
from app.api.admin_session import AdminSession
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.ontology_interview import ask_next, compute_missing, infer_needs, merge_additions
from app.graphrag.ontology_interview_store import (
    InterviewConflictError,
    InterviewExistsError,
    InterviewNotFoundError,
    InvalidInterviewStateError,
    create_session,
    delete_session,
    get_session,
    save_session,
)
from app.providers.registry import ProviderRegistry

router = APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)])


class AnswerRequest(BaseModel):
    # 每一轮都把 turns 整段历史送给模型；一条超长回答一旦写进 state，
    # 之后每一轮都要跟着重新发一遍，且永远留在 state 里删不掉。上限挡在
    # 入口，比事后清理更省事。
    answer: str = Field(max_length=4000)
    updated_at: str


class SaveInterviewRequest(BaseModel):
    # state 是整份骨架 + 对话历史的 dict，没有一个简单的长度约束能覆盖
    # 所有子字段；轮次/骨架规模的上限留后续单独处理，这里不加。
    state: dict
    updated_at: str


class QuestionRequest(BaseModel):
    # 理由同 AnswerRequest.answer：这条文本会被塞进 infer_needs 的 prompt，
    # 超长文本同样值得在入口挡掉。
    text: str = Field(max_length=4000)
    updated_at: str


def _now() -> str:
    return datetime.now().isoformat()


async def _load_or_404(review_conn: aiosqlite.Connection, tenant_id: str):
    session = await get_session(review_conn, tenant_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"租户 {tenant_id} 还没有访谈会话，先开始访谈")
    return session


def _check_lock(session, updated_at: str) -> None:
    # 乐观锁在调模型之前判：调完再发现冲突等于让用户白等一分钟。
    if session.updated_at != updated_at:
        raise HTTPException(
            status_code=409,
            detail=f"访谈在 {session.updated_at} 被 {session.updated_by} 改过，你手上这份是 {updated_at} 的。刷新后重试。",
        )


@router.get("/{tenant_id}/interview")
async def read_interview(tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    session = await get_session(review_conn, tenant_id)
    return {"session": None if session is None else session.to_dict()}


@router.post("/{tenant_id}/interview")
async def start_interview(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        created = await create_session(review_conn, tenant_id, actor=session.username, now=_now())
    except InterviewExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"session": created.to_dict()}


@router.post("/{tenant_id}/interview/answer")
async def answer_interview(
    tenant_id: str,
    payload: AnswerRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    current = await _load_or_404(review_conn, tenant_id)
    # 顺序要紧：先判乐观锁再调模型——调完再发现冲突等于让用户白等一分钟
    # （变异测试锁的就是这一行在 ask_next 之前）。
    _check_lock(current, payload.updated_at)
    answer = payload.answer.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="回答不能为空")

    state = dict(current.state)
    turns = [*state["turns"], {"role": "user", "text": answer}]
    result = await ask_next(
        llm_registry,
        provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        turns=turns,
        skeleton=state["skeleton"],
    )
    if result.question:
        turns.append({"role": "assistant", "text": result.question})
    state["turns"] = turns
    state["skeleton"] = merge_additions(state["skeleton"], result.added)
    state["done"] = state.get("done", False) or result.done
    try:
        saved = await save_session(
            review_conn, tenant_id, state=state, expected_updated_at=current.updated_at,
            actor=session.username, now=_now(),
        )
    except InterviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InterviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    added_count = sum(len(result.added[k]) for k in ("term_types", "relation_types", "constraints"))
    return {
        "session": saved.to_dict(),
        "turn": {"question": result.question, "added_count": added_count, "dropped": result.dropped, "note": result.note},
    }


@router.put("/{tenant_id}/interview")
async def save_interview(
    tenant_id: str,
    payload: SaveInterviewRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        saved = await save_session(
            review_conn, tenant_id, state=payload.state, expected_updated_at=payload.updated_at,
            actor=session.username, now=_now(),
        )
    except InterviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InterviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InvalidInterviewStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"session": saved.to_dict()}


@router.delete("/{tenant_id}/interview")
async def delete_interview(tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await delete_session(review_conn, tenant_id)
    return {"deleted": True}


@router.post("/{tenant_id}/interview/questions")
async def add_interview_question(
    tenant_id: str,
    payload: QuestionRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    current = await _load_or_404(review_conn, tenant_id)
    _check_lock(current, payload.updated_at)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="问题不能为空")
    needs = await infer_needs(llm_registry, provider_name=deps.DEFAULT_LLM_PROVIDER_NAME, question=text, skeleton=current.state["skeleton"])
    missing = compute_missing(needs, current.state["skeleton"])
    entry = {"text": text, "needs": needs, "missing": missing, "at": _now()}
    state = dict(current.state)
    state["questions"] = [*state.get("questions", []), entry]
    try:
        saved = await save_session(
            review_conn, tenant_id, state=state, expected_updated_at=current.updated_at,
            actor=session.username, now=_now(),
        )
    except InterviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InterviewNotFoundError as exc:
        # _load_or_404 读到会话之后、infer_needs 调 LLM 的这几十秒窗口里
        # 会话被删掉，不该裸 500——跟 answer_interview / save_interview 一致处理。
        raise HTTPException(status_code=404, detail=str(exc))
    return {"session": saved.to_dict(), "question": entry}
