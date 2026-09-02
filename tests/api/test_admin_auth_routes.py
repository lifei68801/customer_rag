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
from app.auth.admin_users_store import (
    create_admin_user,
    get_admin_user,
    set_admin_user_status,
)
from app.auth.login_throttle import MAX_FAILURES, LoginThrottle
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

    assert body == {"username": "alice", "role": "member", "tenant_id": "demo"}


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
