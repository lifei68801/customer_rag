from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession, AdminSessionStore
from app.api.session_cookie import (
    SESSION_COOKIE_NAME,
    clear_session_cookies,
    is_secure_request,
    new_csrf_token,
    set_session_cookies,
)
from app.api.tenant_guard import require_active_tenant_or_404
from app.auth.admin_users_store import (
    get_admin_user,
    set_admin_user_password,
    touch_last_login,
)
from app.auth.login_throttle import LoginLockedError, LoginThrottle
from app.auth.password import verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/auth")

#: 三种失败（用户不存在 / 密码错 / 账号已禁用）共用同一条文案和同一个
#: 状态码。区分它们等于把这个接口变成用户名枚举器——攻击者只要看响应
#: 就能列出所有真实存在的账号。原因分别记进服务端日志。
_LOGIN_FAILED_DETAIL = "用户名或密码不正确"


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    session_token: str
    username: str
    role: str
    tenant_id: str | None


class WhoAmIResponse(BaseModel):
    username: str
    role: str
    tenant_id: str | None
    current_tenant_id: str | None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class SwitchTenantRequest(BaseModel):
    tenant_id: str


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    throttle: LoginThrottle = Depends(deps.get_login_throttle),
) -> LoginResponse:
    try:
        throttle.check(payload.username)
    except LoginLockedError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    user = await get_admin_user(review_conn, payload.username)
    # 只记录"发生了失败登录"和原因，绝不记录尝试的密码——日志本身通常比
    # 数据库更容易泄露。
    if user is None:
        logger.warning("管理员登录失败：用户不存在 username=%s", payload.username)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)
    if not verify_password(payload.password, user["password_hash"]):
        # 只在用户确实存在时计数。给任意伪造用户名都建槽位会让内存被撑爆
        # ——这条取舍连同它的局限记在 login_throttle.py 的 docstring 里。
        throttle.record_failure(payload.username)
        logger.warning("管理员登录失败：密码不正确 username=%s", payload.username)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)
    if user["status"] != "active":
        logger.warning("管理员登录失败：账号已停用 username=%s", payload.username)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)

    throttle.record_success(payload.username)
    await touch_last_login(review_conn, payload.username)
    session_token = session_store.create_session(
        username=user["username"], role=user["role"], tenant_id=user["tenant_id"]
    )
    csrf_token = new_csrf_token()
    set_session_cookies(
        response,
        session_token=session_token,
        csrf_token=csrf_token,
        secure=is_secure_request(request),
        max_age=28800,
    )
    return LoginResponse(
        session_token=session_token,
        username=user["username"],
        role=user["role"],
        tenant_id=user["tenant_id"],
    )


@router.get("/whoami", response_model=WhoAmIResponse)
async def whoami(
    session: AdminSession = Depends(deps.require_admin_session),
) -> WhoAmIResponse:
    return WhoAmIResponse(
        username=session.username,
        role=session.role,
        tenant_id=session.tenant_id,
        current_tenant_id=session.current_tenant_id,
    )


@router.put("/password")
async def change_own_password(
    payload: ChangePasswordRequest,
    session: AdminSession = Depends(deps.require_admin_session),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    """改自己的密码，必须验旧密码。

    不验旧密码的话，任何拿到 session 的人（比如一台没锁屏的电脑）都能把
    这个账号锁给自己。
    """
    user = await get_admin_user(review_conn, session.username)
    if user is None or not verify_password(payload.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码不正确")
    try:
        await set_admin_user_password(review_conn, session.username, payload.new_password)
    except ValueError as exc:  # PasswordTooShortError / PasswordTooLongError
        raise HTTPException(status_code=400, detail=str(exc))
    return {"changed": True}


@router.post("/logout", dependencies=[Depends(deps.require_admin_session)])
async def logout(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
) -> dict[str, bool]:
    """让服务端立即失效这个 session token，而不是只靠客户端清 sessionStorage
    /Cookie。

    依赖 require_admin_session 保证走到这里时一定带着合法未过期的凭证
    （Cookie 或 "Bearer <token>"，否则前面已经 401 了）。Cookie 优先于
    Bearer——跟 require_admin_session 取 token 的顺序保持一致，否则浏览器
    端登出时可能撤销错 token（比如同时带着一个过期的 Bearer 头）。
    """
    token = request.cookies.get(SESSION_COOKIE_NAME) or (authorization or "").removeprefix(
        "Bearer "
    )
    session_store.revoke_session(token)
    clear_session_cookies(response)
    return {"logged_out": True}


@router.put(
    "/session/tenant",
    dependencies=[Depends(deps.require_csrf)],
)
async def switch_current_tenant(
    request: Request,
    payload: SwitchTenantRequest,
    session: AdminSession = Depends(deps.require_admin_session),
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    """切换当前租户。

    权限判据复用 require_tenant_access 那一套：admin 可切任意（但仍要确认
    租户启用着），member 只能切回自己那个。
    """
    await require_active_tenant_or_404(review_conn, payload.tenant_id)
    if session.role != "admin" and session.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问该租户")
    token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not session_store.set_current_tenant(token, payload.tenant_id):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return {"tenant_id": payload.tenant_id}
