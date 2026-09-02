from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.main import app
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "correct-token", **overrides})


def test_login_with_correct_token_returns_session_token():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.post("/api/admin/auth/login", json={"admin_token": "correct-token"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "session_token" in response.json()


def test_login_with_wrong_token_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.post("/api/admin/auth/login", json={"admin_token": "wrong"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_login_when_admin_token_not_configured_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings(admin_token=None)
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.post("/api/admin/auth/login", json={"admin_token": "anything"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_admin_protected_route_rejects_missing_token():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.get("/api/admin/auth/whoami")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_admin_protected_route_accepts_valid_session():
    session_store = AdminSessionStore()
    token = session_store.create_session(username="admin", role="admin", tenant_id=None)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/auth/whoami", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_admin_protected_route_rejects_garbage_token():
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/auth/whoami", headers={"Authorization": "Bearer not-a-real-token"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_admin_protected_route_rejects_non_bearer_scheme():
    session_store = AdminSessionStore()
    token = session_store.create_session(username="admin", role="admin", tenant_id=None)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/auth/whoami", headers={"Authorization": f"Basic {token}"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_logout_revokes_the_session_server_side():
    session_store = AdminSessionStore()
    token = session_store.create_session(username="admin", role="admin", tenant_id=None)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    try:
        client = TestClient(app)
        logout_response = client.post(
            "/api/admin/auth/logout", headers={"Authorization": f"Bearer {token}"}
        )
        whoami_response = client.get(
            "/api/admin/auth/whoami", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()

    assert logout_response.status_code == 200
    # 同一个 token 登出之后立刻失效，不用等 TTL 到期
    assert whoami_response.status_code == 401


def test_disabled_account_session_stops_working_immediately(default_admin_users_conn):
    """禁用必须立即生效。

    等 session 自然过期意味着被禁的人还能再操作 8 小时——而禁用的场景通常
    正是"这个人现在就不该再动数据了"。

    用 conftest 那个 autouse 的本体库连接：admin 已经在里面，把它停掉之后
    原来的 session 必须立刻失效。
    """
    import asyncio

    import aiosqlite

    from app.auth.admin_users_store import set_admin_user_status

    conn: aiosqlite.Connection = app.dependency_overrides[deps.get_review_conn]()
    session_store = AdminSessionStore()
    token = session_store.create_session(username="admin", role="admin", tenant_id=None)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    try:
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 200

        asyncio.run(set_admin_user_status(conn, "admin", "disabled"))

        assert client.get("/api/admin/auth/whoami", headers=headers).status_code == 401
    finally:
        app.dependency_overrides.pop(deps.get_settings, None)
        app.dependency_overrides.pop(deps.get_admin_session_store, None)


def test_session_of_a_deleted_account_stops_working(default_admin_users_conn):
    """账号在库里查不到时同样拒绝。

    这条和上一条不同：上一条是 status 变了，这条是行没了（手工清库、
    迁移出错）。若只判 status 而不判"查不到"，一个不存在的用户名配上一个
    还没过期的 session，会被当成有效身份放行。
    """
    import asyncio

    import aiosqlite

    conn: aiosqlite.Connection = app.dependency_overrides[deps.get_review_conn]()
    session_store = AdminSessionStore()
    token = session_store.create_session(username="ghost", role="admin", tenant_id=None)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    try:
        response = TestClient(app).get(
            "/api/admin/auth/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert conn is not None
    finally:
        app.dependency_overrides.pop(deps.get_settings, None)
        app.dependency_overrides.pop(deps.get_admin_session_store, None)
