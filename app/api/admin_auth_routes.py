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
from app.api.deps import list_accessible_tenant_ids
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.tenants_store import list_tenants
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
    await _auto_select_sole_tenant(review_conn, session_store, session_token)
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


async def _auto_select_sole_tenant(
    review_conn: aiosqlite.Connection,
    session_store: AdminSessionStore,
    session_token: str,
) -> None:
    """可访问的 active 租户恰好一个时，登录就把它定下来。

    admin 的 tenant_id 永远是 None（他是跨租户角色，没有一个非任意的"主场"），
    所以 current_tenant_id 初值也是 None，界面会拦一道「先选租户」。**只有一个
    可选项时那不是选择，是噪音**——这个项目在 PersonaRail 里写过同一条规矩。

    多于一个时不替用户决定：admin 的操作都是租户范围内且不可逆的（确认本体、
    导入数据、删组织），替他挑一个是任意的，而且挑错了不报错——界面上一切
    正常，改的是另一个租户的数据。

    已经有 current_tenant_id 的（member 有归属租户）原样不动，不是"按 active
    租户数重新决定"。

    **如实记一句**：这条守卫今天没有任何可达场景能区分它——member 的
    accessible 就是他自己的租户集合，自动选定只可能选中他已经有的那一个，
    值相同、观察不到差别。变异测试里去掉它，四条用例全绿。保留它是因为
    不变量本身成立（"不覆盖用户已有的选择"）且只有一行；但别把它当成
    有测试保护的东西——它没有。

    停用的租户不数进来：库里常年躺着测试残留的停用租户，数进去的话一个实际
    只有一个可用租户的部署永远享受不到自动选定。list_tenants 默认就只返回
    active，这里不传 include_disabled。
    """
    session = session_store.get_session(session_token)
    if session is None or session.current_tenant_id is not None:
        return
    try:
        accessible = await list_accessible_tenant_ids(review_conn, session)
        active = [t["tenant_id"] for t in await list_tenants(review_conn)]
    except Exception:
        # **这层兜底是必须的，不是防御性编程的惯性。**
        #
        # 自动选定只是个便利。把登录挂在 tenants 表的可读性上，等于让一张
        # 表没建好（或者读它出任何错）就没人能登录——为一个便利功能赔上整个
        # 系统的入口。实测踩到过：tenants 表在部分环境里根本不存在，加上这
        # 段之后连 CSRF、Cookie 那些跟租户无关的登录用例一起红了。
        #
        # 失败时退回的是**原有行为**（current_tenant_id 保持 None，界面拦一道
        # 「先选租户」），安全且用户看得见，不是静默降级。
        logger.warning("登录时自动选定租户失败，退回手动选择", exc_info=True)
        return
    # accessible 为 None 表示不设限（admin），此时全部 active 租户都算可访问。
    candidates = active if accessible is None else [t for t in active if t in set(accessible)]
    if len(candidates) == 1:
        session_store.set_current_tenant(session_token, candidates[0])


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


# CSRF 校验不在这里挂：整个 /api/admin/* 在 main.py 的 admin_scoped 上统一
# 挂了一次，两处都写的话，哪天挂载层那份被摘掉，钉这条路由的用例照样绿。
@router.put("/session/tenant")
async def switch_current_tenant(
    request: Request,
    payload: SwitchTenantRequest,
    session: AdminSession = Depends(deps.require_admin_session),
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    """切换当前租户。

    权限判据直接调 deps.assert_tenant_accessible——和租户作用域路由上那个
    require_tenant_access 是同一个函数，不是"同一套逻辑各写一遍"。admin 可
    切任意（但仍要确认租户启用着），member 只能切到被授权的那几个。

    这里不能自己再写一遍判据：写歪了的话切租户接口和读写接口会给出互相
    矛盾的答案——切得过去、进去之后每个请求各自 403，或者更糟的反向。
    """
    await require_active_tenant_or_404(review_conn, payload.tenant_id)
    await deps.assert_tenant_accessible(review_conn, session, payload.tenant_id)
    token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not session_store.set_current_tenant(token, payload.tenant_id):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return {"tenant_id": payload.tenant_id}
