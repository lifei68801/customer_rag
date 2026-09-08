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
from app.auth.user_tenants_store import (
    grant_tenant_access,
    list_granted_tenant_ids,
    revoke_tenant_access,
)
from app.graphrag.tenants_store import TenantNotFoundError, list_tenants, require_active_tenant

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


class AccountTenantsRequest(BaseModel):
    tenant_ids: list[str]


class AccountTenantsResponse(BaseModel):
    tenant_ids: list[str]


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


@router.get("/{username}/tenants", response_model=AccountTenantsResponse)
async def get_account_tenants(
    username: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> AccountTenantsResponse:
    """这个账号被显式授权的租户。admin 账号也能查——查到的多半是空列表，
    因为它从来不需要显式授权；不特殊拒绝 GET，拒绝的是"给 admin 加限制"
    这个写操作本身（见下面的 PUT）。"""
    user = await get_admin_user(review_conn, username)
    if user is None:
        raise HTTPException(status_code=404, detail=f"账号 {username!r} 不存在")
    return AccountTenantsResponse(tenant_ids=await list_granted_tenant_ids(review_conn, username))


@router.put("/{username}/tenants", response_model=AccountTenantsResponse)
async def replace_account_tenants(
    username: str,
    payload: AccountTenantsRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> AccountTenantsResponse:
    """全量替换这个账号的租户授权。

    全量替换而不是追加：界面上是一组复选框，用户取消勾选的那个必须真的被
    撤销。追加语义下"取消勾选"永远不生效，而界面看起来生效了——这正是
    本项目最在意的那类静默失败。

    先校验后写：账号不存在、租户不存在、对象是个 admin、列表为空，四种都
    在写入之前挡住。写一半再失败会留下"一部分授权生效了"的状态，而调用方
    拿到的是一个错误码，他会以为什么都没发生。

    空列表单独拒绝（400），不当成"合法的零授权"放行：
    `deps.list_accessible_tenant_ids` 只有在 user_tenants 里一条记录都没有
    时才回退到 admin_users.tenant_id 那一列。撤销到零和从未授权过在表里
    长得一样——管理员把全部勾去掉保存，他以为收回了全部访问权，这个账号
    却仍能进自己的默认租户，是一次看不见的失败。真要彻底收回访问权限，
    走 `set_admin_user_status` 停用整个账号——那条路已经在账号页上有按钮，
    是明确的、不会被误当成"部分授权"的动作。
    """
    user = await get_admin_user(review_conn, username)
    if user is None:
        raise HTTPException(status_code=404, detail=f"账号 {username!r} 不存在")
    if user["role"] == "admin":
        raise HTTPException(
            status_code=400,
            detail="admin 本来就能访问全部租户，给它单独授权不会产生任何限制效果",
        )
    if not payload.tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="不能保存空列表：撤销到零之后这个账号会回退到默认租户，而不是被"
            "彻底挡在门外。要彻底收回访问权限，请使用账号列表里的「停用账号」。",
        )
    current = set(await list_granted_tenant_ids(review_conn, username))
    # 已有的授权即使指向一个当下已停用的租户，也算「已知」——不然管理员
    # 在停用期间打开这个账号的页面，压根看不见这条授权（前端复选框只会
    # 列出启用中的租户），保存时全量替换会把它当成「没被勾选」悄悄删掉。
    # 这是 2026-09-08 全分支评审 Important 2：管理员以为自己只是点了个
    # 保存，实际撤销了一条他根本不知道存在的授权。只并入 current，不并入
    # 「全部租户」：新授权一个已停用的租户仍然是非法操作，要继续被下面
    # 的 unknown 检查挡住。
    known = {t["tenant_id"] for t in await list_tenants(review_conn)} | current
    unknown = sorted(set(payload.tenant_ids) - known)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"这些租户不存在或已停用：{'、'.join(unknown)}"
        )
    wanted = set(payload.tenant_ids)
    for tenant_id in sorted(wanted - current):
        await grant_tenant_access(review_conn, username=username, tenant_id=tenant_id)
    for tenant_id in sorted(current - wanted):
        await revoke_tenant_access(review_conn, username=username, tenant_id=tenant_id)
    return AccountTenantsResponse(tenant_ids=sorted(wanted))
