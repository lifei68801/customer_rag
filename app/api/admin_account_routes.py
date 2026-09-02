"""账号管理。只有 admin 能用。

账号只禁用不删除：这个系统里的写操作（删文档、批准关系入 Neo4j）不可逆，
账号删了之后"这批数据是谁批准的"就永远查不出来了。
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.auth.admin_users_store import (
    AdminUserAlreadyExistsError,
    AdminUserNotFoundError,
    InvalidUsernameError,
    count_active_admins,
    create_admin_user,
    get_admin_user,
    list_admin_users,
    set_admin_user_password,
    set_admin_user_status,
)
from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant

router = APIRouter(
    prefix="/api/admin/accounts", dependencies=[Depends(deps.require_admin_role)]
)

#: 保留名。允许别人叫 admin 会让"最后一个 admin"这件事变得含糊。
_RESERVED_USERNAMES = {"admin"}


class AccountResponse(BaseModel):
    username: str
    role: str
    tenant_id: str | None
    status: str
    created_at: str
    last_login_at: str | None


class AccountListResponse(BaseModel):
    accounts: list[AccountResponse]


class CreateAccountRequest(BaseModel):
    username: str
    password: str
    tenant_id: str
    # 刻意不接受 role：本设计不提供"再造一个 admin"的入口，多 admin 的
    # 需求出现时再单独设计。现在开这个口子会让"不能禁用最后一个 admin"
    # 那条不变量变复杂而收益为零。Pydantic 默认忽略多余字段，请求体里
    # 塞 role 不会生效。


class ResetPasswordRequest(BaseModel):
    new_password: str


def _public(user: dict) -> AccountResponse:
    return AccountResponse(**{k: v for k, v in user.items() if k != "password_hash"})


@router.get("", response_model=AccountListResponse)
async def list_accounts(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> AccountListResponse:
    users = await list_admin_users(review_conn)
    return AccountListResponse(accounts=[AccountResponse(**u) for u in users])


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    payload: CreateAccountRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> AccountResponse:
    if payload.username in _RESERVED_USERNAMES:
        raise HTTPException(status_code=400, detail=f"用户名已被保留：{payload.username}")
    try:
        # 建给不存在或已停用的租户，那个账号登录后会看到一片空白，且没人
        # 说得出为什么。
        await require_active_tenant(review_conn, payload.tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=400, detail="租户不存在或未启用")
    try:
        await create_admin_user(
            review_conn,
            username=payload.username,
            password=payload.password,
            role="member",
            tenant_id=payload.tenant_id,
        )
    except (AdminUserAlreadyExistsError, InvalidUsernameError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:  # 密码长度
        raise HTTPException(status_code=400, detail=str(exc))
    created = await get_admin_user(review_conn, payload.username)
    return _public(created)


@router.post("/{username}/disable")
async def disable_account(
    username: str,
    session: AdminSession = Depends(deps.require_admin_role),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    # 一次误点就把自己锁在门外，只能手改数据库救。
    if username == session.username:
        raise HTTPException(status_code=400, detail="不能停用自己")
    user = await get_admin_user(review_conn, username)
    if user is None:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    # 在本设计下这条是上一条的子集（只有一个 admin，且不提供创建 admin 的
    # 入口），有意保留：将来若开放多 admin，这条不必重新想起来。
    if user["role"] == "admin" and await count_active_admins(review_conn) <= 1:
        raise HTTPException(status_code=400, detail="不能停用最后一个管理员")
    await set_admin_user_status(review_conn, username, "disabled")
    return {"disabled": True}


@router.post("/{username}/enable")
async def enable_account(
    username: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    try:
        await set_admin_user_status(review_conn, username, "active")
    except AdminUserNotFoundError:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    return {"enabled": True}


@router.put("/{username}/password")
async def reset_password(
    username: str,
    payload: ResetPasswordRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    """重置他人密码，不需要旧密码——这个接口就是给"忘了密码"用的。"""
    try:
        await set_admin_user_password(review_conn, username, payload.new_password)
    except AdminUserNotFoundError:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"changed": True}
