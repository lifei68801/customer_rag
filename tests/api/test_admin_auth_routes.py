"""管理后台登录：用户名 + 密码。

旧的 admin_token 单字段登录路径已经删除，不保留双轨——两条鉴权路径同时
存在，加固时总有一条会被忘记，而被忘记的那条就是活的越权通道。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.api.session_cookie import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from app.auth.admin_users_store import (
    create_admin_user,
    get_admin_user,
    set_admin_user_status,
)
from app.auth.login_throttle import MAX_FAILURES, LoginThrottle
from app.graphrag.tenants_store import create_tenants_table
from app.main import app
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "seed-secret", **overrides})


def _conn() -> aiosqlite.Connection:
    """conftest 那个 autouse fixture 装好的本体库，里面已经有 admin。"""
    return app.dependency_overrides[deps.get_review_conn]()


def _seed_member(conn: aiosqlite.Connection, *, username="alice", password="password1") -> None:
    asyncio.run(
        create_admin_user(
            conn, username=username, password=password, role="member", tenant_id="demo"
        )
    )


def _seed_tenant_row(conn: aiosqlite.Connection, tenant_id: str, *, status: str = "active") -> None:
    """往 review_conn 里补一张 tenants 表并插一行。

    这张表默认不存在——default_admin_users_conn 那个 autouse fixture 只建了
    admin_users，只有真正要碰 switch_current_tenant（走 require_active_tenant_or_404）
    的测试才需要它，照 test_admin_tenant_routes.py 的 create_tenants_table() 用法来，
    不是自己发明一套 schema。
    """

    async def _do() -> None:
        await create_tenants_table(conn)
        await conn.execute(
            "INSERT OR REPLACE INTO tenants (tenant_id, name, status) VALUES (?, ?, ?)",
            (tenant_id, tenant_id, status),
        )
        await conn.commit()

    asyncio.run(_do())


def _clear_overrides() -> None:
    for dep in (deps.get_settings, deps.get_admin_session_store, deps.get_login_throttle):
        app.dependency_overrides.pop(dep, None)


def _login(*, username: str, password: str, throttle: LoginThrottle | None = None):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    if throttle is not None:
        app.dependency_overrides[deps.get_login_throttle] = lambda: throttle
    try:
        return TestClient(app).post(
            "/api/admin/auth/login", json={"username": username, "password": password}
        )
    finally:
        _clear_overrides()


def _login_with_client(*, username: str = "admin", password: str = "password1"):
    """跟 _login 一样 override 依赖，但把 TestClient 也一并返回、不立即清
    override——Cookie 会话要靠同一个 TestClient 的 cookie jar 才能跨请求
    带上，_login() 每次都新建一个 TestClient 是故意的（互不干扰），但这里
    恰恰需要反过来。调用方要在自己的 finally 里调 _clear_overrides()。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    client = TestClient(app)
    response = client.post(
        "/api/admin/auth/login", json={"username": username, "password": password}
    )
    return client, response


def _authed(username: str, role: str, tenant_id: str | None):
    session_store = AdminSessionStore()
    token = session_store.create_session(username=username, role=role, tenant_id=tenant_id)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    return TestClient(app), {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def test_login_with_correct_credentials_returns_identity():
    """登录响应要带上身份。前端据此决定渲染什么——但渲染不承担安全责任，
    真正的门在后端的租户校验上。"""
    _seed_member(_conn())

    response = _login(username="alice", password="password1")

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "alice"
    assert body["role"] == "member"
    assert body["tenant_id"] == "demo"
    assert body["session_token"]


def test_admin_login_reports_no_tenant():
    """admin 不属于任何租户。返回一个具体租户会让前端以为它被绑定了。"""
    body = _login(username="admin", password="password1").json()

    assert body["role"] == "admin"
    assert body["tenant_id"] is None


@pytest.mark.parametrize(
    "username,password",
    [
        ("alice", "wrongpassword"),  # 密码错
        ("nobody", "password1"),  # 用户不存在
    ],
)
def test_failed_login_does_not_reveal_which_part_was_wrong(username: str, password: str):
    """三种失败（用户不存在 / 密码错 / 账号禁用）必须返回同一条文案和同一个
    状态码。区分它们等于把这个接口变成用户名枚举器。"""
    _seed_member(_conn())

    response = _login(username=username, password=password)

    assert response.status_code == 401
    assert response.json()["detail"] == "用户名或密码不正确"


def test_disabled_account_login_looks_the_same_as_a_wrong_password():
    conn = _conn()
    _seed_member(conn)
    asyncio.run(set_admin_user_status(conn, "alice", "disabled"))

    response = _login(username="alice", password="password1")

    assert response.status_code == 401
    assert response.json()["detail"] == "用户名或密码不正确"


def test_login_updates_last_login():
    conn = _conn()
    _seed_member(conn)

    _login(username="alice", password="password1")

    assert asyncio.run(get_admin_user(conn, "alice"))["last_login_at"] is not None


def test_password_is_never_echoed_back():
    """响应体里绝不能出现提交的密码或它的哈希。"""
    _seed_member(_conn())

    text = _login(username="alice", password="password1").text

    assert "password1" not in text
    assert "scrypt$" not in text


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------


def test_attempt_after_max_failures_is_throttled():
    """密码登录比 token 登录容易爆破得多，没有限流就是敞开的门。"""
    _seed_member(_conn())
    throttle = LoginThrottle()

    for _ in range(MAX_FAILURES):
        assert _login(username="alice", password="bad", throttle=throttle).status_code == 401

    assert _login(username="alice", password="bad", throttle=throttle).status_code == 429


def test_throttle_blocks_even_the_correct_password():
    """锁定期间正确密码也进不去。否则限流只挡住了打错密码的人，挡不住
    正在爆破的人——他迟早会试对。"""
    _seed_member(_conn())
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES):
        _login(username="alice", password="bad", throttle=throttle)

    assert _login(username="alice", password="password1", throttle=throttle).status_code == 429


def test_successful_login_clears_the_counter():
    """打错几次然后打对了，不该在下次登录时留着旧账。"""
    _seed_member(_conn())
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES - 1):
        _login(username="alice", password="bad", throttle=throttle)

    assert _login(username="alice", password="password1", throttle=throttle).status_code == 200

    for _ in range(MAX_FAILURES - 1):
        assert _login(username="alice", password="bad", throttle=throttle).status_code == 401


def test_one_user_being_throttled_does_not_lock_out_another():
    """按 username 计数而不是全局：否则一个攻击者能顺带把所有人锁在门外。"""
    _seed_member(_conn())
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES):
        _login(username="alice", password="bad", throttle=throttle)

    assert _login(username="admin", password="password1", throttle=throttle).status_code == 200


# ---------------------------------------------------------------------------
# whoami / 改自己的密码 / 登出
# ---------------------------------------------------------------------------


def test_whoami_returns_identity():
    _seed_member(_conn())
    client, headers = _authed("alice", "member", "demo")
    try:
        body = client.get("/api/admin/auth/whoami", headers=headers).json()
    finally:
        _clear_overrides()

    assert body == {
        "username": "alice",
        "role": "member",
        "tenant_id": "demo",
        "current_tenant_id": "demo",
    }


def test_whoami_requires_a_session():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        assert TestClient(app).get("/api/admin/auth/whoami").status_code == 401
    finally:
        _clear_overrides()


def test_whoami_rejects_non_bearer_scheme():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        response = TestClient(app).get(
            "/api/admin/auth/whoami", headers={"Authorization": "Basic abc"}
        )
        assert response.status_code == 401
    finally:
        _clear_overrides()


def test_can_change_own_password_with_the_old_one():
    _seed_member(_conn())
    client, headers = _authed("alice", "member", "demo")
    try:
        response = client.put(
            "/api/admin/auth/password",
            json={"old_password": "password1", "new_password": "password2"},
            headers=headers,
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert _login(username="alice", password="password2").status_code == 200


def test_cannot_change_password_without_the_old_one():
    """不验旧密码的话，任何拿到 session 的人（比如一台没锁屏的电脑）都能
    把这个账号锁给自己。"""
    _seed_member(_conn())
    client, headers = _authed("alice", "member", "demo")
    try:
        response = client.put(
            "/api/admin/auth/password",
            json={"old_password": "wrongpassword", "new_password": "password2"},
            headers=headers,
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    # 旧密码仍然有效——失败的改密不能把账号改成一个谁都不知道的状态。
    assert _login(username="alice", password="password1").status_code == 200


def test_too_short_new_password_is_rejected():
    _seed_member(_conn())
    client, headers = _authed("alice", "member", "demo")
    try:
        response = client.put(
            "/api/admin/auth/password",
            json={"old_password": "password1", "new_password": "short"},
            headers=headers,
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert _login(username="alice", password="password1").status_code == 200


def test_logout_revokes_the_session_server_side():
    """只清客户端 sessionStorage 的话，那个 token 还能用 8 小时——从一台
    公用电脑上登出之后尤其危险。"""
    client, headers = _authed("admin", "admin", None)
    try:
        assert client.post("/api/admin/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 401
    finally:
        _clear_overrides()


# ---------------------------------------------------------------------------
# 禁用立即生效
# ---------------------------------------------------------------------------


def test_disabled_account_session_stops_working_immediately():
    """禁用必须立即生效。等 session 自然过期意味着被禁的人还能再操作
    8 小时——而禁用的场景通常正是"这个人现在就不该再动数据了"。"""
    conn = _conn()
    _seed_member(conn)
    client, headers = _authed("alice", "member", "demo")
    try:
        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 200

        asyncio.run(set_admin_user_status(conn, "alice", "disabled"))

        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 401
    finally:
        _clear_overrides()


def test_session_of_a_deleted_account_stops_working():
    """账号在库里查不到时同样拒绝。

    这条和上一条不同：上一条是 status 变了，这条是行没了（手工清库、迁移
    出错）。若只判 status 而不判"查不到"，一个不存在的用户名配上一个还没
    过期的 session，会被当成有效身份放行。
    """
    client, headers = _authed("ghost", "admin", None)
    try:
        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 401
    finally:
        _clear_overrides()


# ---------------------------------------------------------------------------
# 会话 Cookie / CSRF
# ---------------------------------------------------------------------------


def test_login_sets_session_and_csrf_cookies():
    client, response = _login_with_client()
    try:
        assert response.status_code == 200
        assert SESSION_COOKIE_NAME in response.cookies
        assert CSRF_COOKIE_NAME in response.cookies
    finally:
        _clear_overrides()


def test_cookie_session_is_accepted_without_authorization_header():
    """装上 Cookie 之后，同源请求不再需要手工带 Bearer 头——这正是
    前台走到后台不用二次登录的机制。"""
    client, _ = _login_with_client()
    try:
        response = client.get("/api/admin/auth/whoami")
        assert response.status_code == 200
        assert response.json()["username"] == "admin"
    finally:
        _clear_overrides()


def test_whoami_returns_current_tenant_id():
    _seed_member(_conn())
    client, _ = _login_with_client(username="alice", password="password1")
    try:
        body = client.get("/api/admin/auth/whoami").json()
    finally:
        _clear_overrides()

    assert body["current_tenant_id"] == "demo"


def test_logout_revokes_the_token_server_side_not_only_the_cookie():
    """只清 Cookie 不够：token 还活在服务端字典里，8 小时内谁拿到它仍然
    有效。登出必须两件事都做。"""
    client, login_response = _login_with_client()
    try:
        token = login_response.json()["session_token"]
        client.post("/api/admin/auth/logout")

        # 用原来那个 token 直接敲（绕开 Cookie），服务端应当已经不认了
        response = client.get(
            "/api/admin/auth/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
    finally:
        _clear_overrides()


def test_write_request_without_csrf_header_is_rejected():
    client, _ = _login_with_client()
    try:
        response = client.put(
            "/api/admin/auth/session/tenant", json={"tenant_id": "demo"}
        )
        assert response.status_code == 403
    finally:
        _clear_overrides()


def test_valid_cookie_wins_over_invalid_bearer_header():
    """取 token 顺序必须是先 Cookie 后 Bearer：浏览器场景下 Cookie 才是真正
    在用的凭证，一个过期/伪造的 Bearer 头（比如浏览器缓存里剩下的旧值）
    不该把它顶掉。这条防的是"以后有人顺手把顺序调过来"——调过来之后，
    单独测 Cookie 或单独测 Bearer 的用例都还是绿的，只有两者同时出现、
    Bearer 是假的这种组合才会露馅。"""
    client, _ = _login_with_client()
    try:
        response = client.get(
            "/api/admin/auth/whoami",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "admin"
    finally:
        _clear_overrides()


# ---------------------------------------------------------------------------
# 切换当前租户
# ---------------------------------------------------------------------------


def test_switch_tenant_success_updates_current_tenant_id():
    """成功路径要连带验证 whoami 里真的变了——只看 200 会漏掉"返回了
    正确响应但压根没写进会话"这种半成品实现。"""
    conn = _conn()
    _seed_tenant_row(conn, "demo")
    client, _ = _login_with_client()
    try:
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        response = client.put(
            "/api/admin/auth/session/tenant",
            json={"tenant_id": "demo"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert response.status_code == 200
        assert response.json() == {"tenant_id": "demo"}

        whoami = client.get("/api/admin/auth/whoami").json()
        assert whoami["current_tenant_id"] == "demo"
    finally:
        _clear_overrides()


def test_switch_tenant_member_cannot_switch_to_another_tenant():
    conn = _conn()
    _seed_member(conn)
    _seed_tenant_row(conn, "demo")
    _seed_tenant_row(conn, "other")
    client, _ = _login_with_client(username="alice", password="password1")
    try:
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        response = client.put(
            "/api/admin/auth/session/tenant",
            json={"tenant_id": "other"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert response.status_code == 403
    finally:
        _clear_overrides()


def test_switch_tenant_to_unregistered_tenant_is_404():
    conn = _conn()
    _seed_tenant_row(conn, "demo")
    client, _ = _login_with_client()
    try:
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        response = client.put(
            "/api/admin/auth/session/tenant",
            json={"tenant_id": "does-not-exist"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert response.status_code == 404
    finally:
        _clear_overrides()
