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
    token = session_store.create_session()
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
    token = session_store.create_session()
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
    token = session_store.create_session()
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
