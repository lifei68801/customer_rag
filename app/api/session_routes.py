from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.memory.chat_sessions import delete_session, list_sessions
from app.memory.session_window import get_recent_turns

# require_csrf 挂在 router 上而不是逐个路由：漏挂一个写接口不会有任何测试
# 变红，而漏掉的那一个就是活的 CSRF 通道。
#
# get_gateway_tenant_id 留在这里只当"网关凭证校验"用，不再参与租户解析：
# 身份改从会话取之后，配置了 gateway_shared_secret 的部署仍然必须带上有效的
# X-Gateway-Secret 才进得来（test_qa_routes.py 与 test_agent_chat_routes.py 里
# 的 rejects_wrong_gateway_secret 用例钉的就是这条路径还活着）。
router = APIRouter(
    dependencies=[Depends(deps.require_csrf), Depends(deps.get_gateway_tenant_id)]
)

# 左边栏历史消息一次最多加载这么多条——客服问答场景单会话一般不会长到
# 需要分页，超过这个数量的早期消息就看不到了，是刻意的范围缩减。
_MESSAGE_HISTORY_LIMIT = 50


class SessionSummary(BaseModel):
    session_id: str
    title: str
    created_at: str
    updated_at: str


class ListSessionsResponse(BaseModel):
    sessions: list[SessionSummary]


class SessionMessage(BaseModel):
    role: str
    content: str
    created_at: str


class SessionMessagesResponse(BaseModel):
    messages: list[SessionMessage]


@router.get("/agent/sessions", response_model=ListSessionsResponse)
async def list_sessions_endpoint(
    identity: tuple[str, str] = Depends(deps.require_chat_session),
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
) -> ListSessionsResponse:
    tenant_id, user_id = identity
    rows = await list_sessions(memory_conn, tenant_id=tenant_id, user_id=user_id)
    return ListSessionsResponse(sessions=[SessionSummary(**row) for row in rows])


@router.get("/agent/sessions/{session_id}/messages", response_model=SessionMessagesResponse)
async def get_session_messages_endpoint(
    session_id: str,
    identity: tuple[str, str] = Depends(deps.require_chat_session),
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
) -> SessionMessagesResponse:
    tenant_id, _user_id = identity
    # 不额外校验 session_id 是否真的属于这个 user_id——get_recent_turns 本身
    # 按 tenant_id+session_id 查询，猜中别人的 session_id 就能读到内容的
    # 权限模型和 /agent/chat 现有的完全一致（session_id 本身即凭证，见
    # 会话侧边栏设计讨论），这里不引入新的更严格校验。
    turns = await get_recent_turns(
        memory_conn,
        tenant_id=tenant_id,
        session_id=session_id,
        limit=_MESSAGE_HISTORY_LIMIT,
    )
    return SessionMessagesResponse(messages=[SessionMessage(**turn) for turn in turns])


@router.delete("/agent/sessions/{session_id}")
async def delete_session_endpoint(
    session_id: str,
    identity: tuple[str, str] = Depends(deps.require_chat_session),
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
) -> dict[str, bool]:
    tenant_id, user_id = identity
    deleted = await delete_session(
        memory_conn, tenant_id=tenant_id, user_id=user_id, session_id=session_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True}
