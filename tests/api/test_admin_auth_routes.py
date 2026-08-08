from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.main import app


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        admin_token="correct-token",
    )
    defaults.update(overrides)
    return Settings(**defaults)


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
