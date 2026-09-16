from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema, replace_draft
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app

pytestmark = pytest.mark.anyio

AUTH = {"Authorization": "Bearer x"}


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    await create_tenants_table(conn)
    for tid in ("t1", "t2"):
        await create_tenant(conn, tenant_id=tid, name=tid)
    return conn


@pytest.fixture
def conn_holder() -> dict:
    return {}


@pytest.fixture
def client(conn_holder):
    async def _get_conn():
        if "conn" not in conn_holder:
            conn_holder["conn"] = await _review_conn()
        return conn_holder["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    # 用 admin 身份，跟 tests/api/test_admin_ontology_routes.py 的 _fake_admin_session
    # 一致：这些用例关心的是路由逻辑，不是权限。member 走 require_tenant_access 会去读
    # user_tenants 表（这个手工建表的测试连接里没有），而且访问未授权租户返回的是 403，
    # 会把 test_unknown_tenant_is_404 这条的语义搅乱。
    # 「工作台对 member 开放」这条性质不靠这里保证：新路由挂在 tenant_scoped 下、
    # 没有 require_admin_role，而 tenant_scoped 里的路由按定义就不要求 admin 角色；
    # 界面一侧由 workbenchPage.test.tsx 的「member 也能用」那条盯着。
    app.dependency_overrides[deps.require_admin_session] = lambda: AdminSession(
        username="alice", role="admin", tenant_id=None, expires_at=1e18
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_lists_builtin_skills(client):
    resp = client.get("/api/admin/ontology/t1/modeling-workspace/skills", headers=AUTH)
    assert resp.status_code == 200
    skills = resp.json()["skills"]
    retail = next(s for s in skills if s["name"] == "consumer_retail")
    assert retail["display_name"] == "消费品零售"
    assert any(t["value"] == "SKU" for t in retail["term_types"])


def test_get_absent_workspace_returns_null_not_404(client):
    """404 会被 adminFetch 的调用方当成"这个租户不存在"；"还没建工作区"是
    工作台的正常首屏状态，不是错误。"""
    resp = client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"workspace": None}


def test_create_read_save_round_trip(client):
    created = client.post(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"skill_name": "consumer_retail"},
        headers=AUTH,
    )
    assert created.status_code == 200
    workspace = created.json()["workspace"]
    assert workspace["skill_name"] == "consumer_retail"
    assert workspace["updated_by"] == "alice"
    assert any(t["value"] == "SKU" for t in workspace["state"]["term_types"])

    state = workspace["state"]
    state["term_types"][0]["review"] = "accepted"
    saved = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": state, "updated_at": workspace["updated_at"]},
        headers=AUTH,
    )
    assert saved.status_code == 200
    assert saved.json()["workspace"]["state"]["term_types"][0]["review"] == "accepted"

    again = client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)
    assert again.json()["workspace"]["state"]["term_types"][0]["review"] == "accepted"


def test_create_blank_workspace(client):
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["workspace"]["skill_name"] is None
    assert resp.json()["workspace"]["state"]["term_types"] == []


def test_create_with_unknown_skill_is_400(client):
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": "nope"}, headers=AUTH
    )
    assert resp.status_code == 400
    assert "nope" in resp.json()["detail"]


def test_create_twice_is_409(client):
    client.post("/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH)
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    )
    assert resp.status_code == 409


def test_save_with_stale_updated_at_is_409(client):
    created = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    ).json()["workspace"]
    client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": created["state"], "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    resp = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": created["state"], "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 409


def test_save_absent_workspace_is_404(client):
    resp = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={"state": {"term_types": []}, "updated_at": "whatever"},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_save_malformed_state_is_400(client):
    created = client.post(
        "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
    ).json()["workspace"]
    resp = client.put(
        "/api/admin/ontology/t1/modeling-workspace",
        json={
            "state": {"term_types": [{"value": "SKU", "provenance": "llm", "review": "pending"}]},
            "updated_at": created["updated_at"],
        },
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert "llm" in resp.json()["detail"]


def test_delete_then_recreate(client):
    client.post("/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH)
    assert client.delete("/api/admin/ontology/t1/modeling-workspace", headers=AUTH).status_code == 200
    assert client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH).json()["workspace"] is None
    assert (
        client.post(
            "/api/admin/ontology/t1/modeling-workspace", json={"skill_name": None}, headers=AUTH
        ).status_code
        == 200
    )


async def test_grounding_reflects_the_mapping(client, conn_holder):
    client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)  # 建连接
    conn = conn_holder["conn"]
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
        etl_mapping={
            "config_yaml": (
                "tenant_id: t1\nentities:\n  - term_type: SKU\n    source_file: sku.xls\n"
                "    standard_name_column: n\n    node_key_parts:\n      - column: jan\n"
                "relations: []\n"
            ),
            "source_file_name": "sku.xls",
        },
        actor="alice",
    )
    resp = client.get("/api/admin/ontology/t1/modeling-workspace/grounding", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["grounded_term_types"] == ["SKU"]
    assert resp.json()["status"] == "draft"


def test_apply_preview_returns_a_diff_without_writing(client):
    payload = {
        "term_types": [{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        "relation_types": [],
        "constraints": [],
    }
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace/apply-preview", json=payload, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["added_term_types"] == ["SKU"]
    # 预览不写库：再查一次草稿，SKU 不该在里面
    listed = client.get("/api/admin/ontology/t1/term-types?status=draft", headers=AUTH)
    assert all(t["value"] != "SKU" for t in listed.json()["term_types"])


async def test_export_skill_returns_yaml(client, conn_holder):
    client.get("/api/admin/ontology/t1/modeling-workspace", headers=AUTH)
    conn = conn_holder["conn"]
    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "SKU", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
        actor="alice",
    )
    await confirm_ontology(conn, "t1", actor="alice")
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace/export-skill",
        json={"skill_name": "t1_domain", "display_name": "T1 领域"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert "name: t1_domain" in resp.json()["yaml"]


def test_export_without_confirmed_ontology_is_400(client):
    resp = client.post(
        "/api/admin/ontology/t1/modeling-workspace/export-skill",
        json={"skill_name": "t1_domain", "display_name": "T1 领域"},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_unknown_tenant_is_404(client):
    resp = client.get("/api/admin/ontology/nope/modeling-workspace", headers=AUTH)
    assert resp.status_code == 404
