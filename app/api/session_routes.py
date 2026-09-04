from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.memory.chat_sessions import delete_session, list_sessions, session_belongs_to_user
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
    tenant_id, user_id = identity
    # 先校验归属再读内容："session_id 本身即凭证"那套在前台还是匿名的时候
    # 尚且说得过去，装上登录门之后就不成立了：同租户的另一个坐席登录后拿到
    # 一个 session_id 就能读到别人的整段对话。归属信息在 chat_sessions 里
    # 现成，同文件的 list/delete 都用了它。
    #
    # 不属于自己一律 404、和 delete_session_endpoint 用同一个说法："不存在"
    # 和"不是你的"因此对调用方不可区分，403 那种写法反而额外告诉对方"这个
    # id 确实存在"。
    if not await session_belongs_to_user(
        memory_conn, tenant_id=tenant_id, user_id=user_id, session_id=session_id
    ):
        raise HTTPException(status_code=404, detail="会话不存在")
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
