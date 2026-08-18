from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.tenants_store import create_tenants_table
from app.main import app

pytestmark = pytest.mark.anyio


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await create_tenants_table(conn)
    return conn


@pytest.fixture
def conn_for_testing() -> dict[str, aiosqlite.Connection]:
    """Holder for connection shared between client fixture and tests."""
    return {}


@pytest.fixture
def client(conn_for_testing):
    async def _get_conn():
        if "conn" not in conn_for_testing:
            conn_for_testing["conn"] = await _review_conn()
        return conn_for_testing["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _tenant_ids(resp) -> set[str]:
    return {t["tenant_id"] for t in resp.json()["tenants"]}


async def test_list_tenants_defaults_to_active_only(client, conn_for_testing):
    conn_for_testing["conn"] = await _review_conn()
    await conn_for_testing["conn"].execute(
        "INSERT INTO tenants (tenant_id, name, status) VALUES (?, ?, ?)",
        ("active_tenant", "Active Tenant", "active"),
    )
    await conn_for_testing["conn"].execute(
        "INSERT INTO tenants (tenant_id, name, status) VALUES (?, ?, ?)",
        ("disabled_tenant", "Disabled Tenant", "disabled"),
    )
    await conn_for_testing["conn"].commit()

    resp = client.get("/api/admin/tenants", headers={"Authorization": "Bearer x"})

    assert resp.status_code == 200
    ids = _tenant_ids(resp)
    assert "active_tenant" in ids
    assert "disabled_tenant" not in ids


def test_create_tenant_then_list_shows_it(client):
    resp = client.post(
        "/api/admin/tenants",
        json={"tenant_id": "new_tenant", "name": "New Tenant"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 201
    assert resp.json() == {"tenant_id": "new_tenant", "name": "New Tenant", "status": "active"}

    resp = client.get("/api/admin/tenants", headers={"Authorization": "Bearer x"})
    assert "new_tenant" in _tenant_ids(resp)


def test_create_tenant_duplicate_id_returns_400(client):
    resp = client.post(
        "/api/admin/tenants",
        json={"tenant_id": "dup_tenant", "name": "Dup Tenant"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 201

    resp = client.post(
        "/api/admin/tenants",
        json={"tenant_id": "dup_tenant", "name": "Another Name"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400


def test_create_tenant_invalid_tenant_id_returns_422(client):
    resp = client.post(
        "/api/admin/tenants",
        json={"tenant_id": "bad/id", "name": "Bad Tenant"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 422


def test_disable_then_enable_tenant(client):
    client.post(
        "/api/admin/tenants",
        json={"tenant_id": "toggle_tenant", "name": "Toggle Tenant"},
        headers={"Authorization": "Bearer x"},
    )

    resp = client.post(
        "/api/admin/tenants/toggle_tenant/disable", headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "disabled"}

    resp = client.get("/api/admin/tenants", headers={"Authorization": "Bearer x"})
    assert "toggle_tenant" not in _tenant_ids(resp)

    resp = client.post(
        "/api/admin/tenants/toggle_tenant/enable", headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "active"}

    resp = client.get("/api/admin/tenants", headers={"Authorization": "Bearer x"})
    assert "toggle_tenant" in _tenant_ids(resp)


def test_disable_nonexistent_tenant_returns_404(client):
    resp = client.post(
        "/api/admin/tenants/does_not_exist/disable", headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 404


def test_enable_nonexistent_tenant_returns_404(client):
    resp = client.post(
        "/api/admin/tenants/does_not_exist/enable", headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 404
