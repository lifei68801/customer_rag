from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.tenants_store import create_tenants_table
from app.main import app


def _fake_admin_session() -> AdminSession:
    """跳过鉴权用的假身份。

    以前这里是 `lambda: None`——那时 require_admin_session 的返回值没人用。
    现在 require_admin_role 要读它的 role，返回 None 会让每条请求都撞上
    AttributeError。这些测试关心的是路由逻辑，不是权限。
    """
    return AdminSession(username="admin", role="admin", tenant_id=None, expires_at=1e18)



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
    app.dependency_overrides[deps.require_admin_session] = _fake_admin_session
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


async def test_list_can_include_disabled_tenants(client, conn_for_testing):
    """租户管理页要能看到停用的——看不到就没法启用它们。

    默认值仍然只返回启用中的（见上一条）：账号菜单的切换下拉框用的就是那个
    默认值，列出停用的租户会让用户切过去之后发现读得到、写全是 404
    （tenant_guard 那条"读放行、写不放行"的策略），那是最难查的一类状态。
    """
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

    resp = client.get(
        "/api/admin/tenants",
        params={"include_disabled": "true"},
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 200
    by_id = {t["tenant_id"]: t for t in resp.json()["tenants"]}
    assert by_id["active_tenant"]["status"] == "active"
    assert by_id["disabled_tenant"]["status"] == "disabled"
