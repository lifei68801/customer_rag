from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app

pytestmark = pytest.mark.anyio


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    # ensure_ontology_schema 只建分类/关系类型/约束三张表——这个 fixture 还需要
    # terms 表存在，因为 test_delete_term_type_in_use_returns_409 要通过
    # /api/admin/terms 创建一条术语来制造"分类在用"的场景（terms 表由 Task 6 的
    # terms_store.py 管理，不在 ontology_lifecycle 的统一建表入口里）。
    await ensure_terms_schema(conn)
    return conn


class _FakeGraphClient:
    """占位图谱客户端——本文件里唯一需要真实 Neo4j 写入的场景是
    test_delete_term_type_in_use_returns_409 借道 /api/admin/terms 创建
    一条术语来制造"分类在用"的场景，那条路由依赖 deps.get_graph_client
    做 sync_term()。这里只需要 sync_term 不抛异常，不需要记录调用。

    migrate_relation_type_edges 支持可配置返回值/异常，供迁移路由的测试
    直接构造一个带指定行为的实例覆盖 fixture 里的默认值。"""

    def __init__(self, *, migrated_count: int = 0, migrate_error: Exception | None = None) -> None:
        self._migrated_count = migrated_count
        self._migrate_error = migrate_error

    async def sync_term(self, term) -> None:
        pass

    async def migrate_relation_type_edges(self, *, tenant_id: str, old_type: str, new_type: str) -> int:
        if self._migrate_error is not None:
            raise self._migrate_error
        return self._migrated_count


@pytest.fixture
def client(monkeypatch):
    conn_holder: dict[str, aiosqlite.Connection] = {}

    async def _get_conn():
        if "conn" not in conn_holder:
            conn_holder["conn"] = await _review_conn()
        return conn_holder["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_and_list_term_types(client):
    resp = client.post(
        "/api/admin/ontology/term-types",
        json={"value": "错误码", "extra_fields": ["严重等级"]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/term-types", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    assert resp.json() == {"term_types": [{"value": "错误码", "extra_fields": ["严重等级"]}]}


def test_delete_term_type_in_use_returns_409(client):
    client.post(
        "/api/admin/ontology/term-types", json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/product-lines", json={"value": "示例产品线"},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/terms",
        json={"standard_name": "x", "aliases": [], "term_type": "错误码", "product_line": "示例产品线"},
        headers={"Authorization": "Bearer x"},
    )

    resp = client.delete("/api/admin/ontology/term-types/错误码", headers={"Authorization": "Bearer x"})

    assert resp.status_code == 409


def test_checkout_confirm_and_list_relation_types(client):
    resp = client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/relation-types?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert len(resp.json()["relation_types"]) == 10

    resp = client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/relation-types?status=confirmed", headers={"Authorization": "Bearer x"}
    )
    assert len(resp.json()["relation_types"]) == 10


def test_create_relation_type_rejects_bad_name(client):
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.post(
        "/api/admin/ontology/t1/relation-types",
        json={"relation_type": "bad-name", "example_phrase": "x"},
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 400


def test_add_and_list_constraints(client):
    client.post(
        "/api/admin/ontology/term-types", json={"value": "客房", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/term-types", json={"value": "酒店", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.post(
        "/api/admin/ontology/t1/constraints",
        json={"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/constraints?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert resp.json()["constraints"] == [
        {"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"}
    ]


def test_remove_constraint_via_delete_with_body(client):
    """DELETE /{tenant_id}/constraints 带 body——确认 TestClient 真的能把
    body 发送到一个 DELETE 请求上，路由端能正常解析。"""
    client.post(
        "/api/admin/ontology/term-types", json={"value": "客房", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/term-types", json={"value": "酒店", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})
    client.post(
        "/api/admin/ontology/t1/constraints",
        json={"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"},
        headers={"Authorization": "Bearer x"},
    )

    resp = client.request(
        "DELETE", "/api/admin/ontology/t1/constraints",
        json={"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/constraints?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert resp.json()["constraints"] == []


def test_delete_tenant_relation_type_route(client):
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.delete(
        "/api/admin/ontology/t1/relation-types/PRECEDES", headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/relation-types?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert "PRECEDES" not in {r["relation_type"] for r in resp.json()["relation_types"]}


def test_update_tenant_relation_type_route_renames(client):
    """PUT 的 body 带一个跟路径不同的 relation_type——必须真的改名，
    后续 GET 应该看到新名字而不是旧名字。"""
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.put(
        "/api/admin/ontology/t1/relation-types/PRECEDES",
        json={
            "relation_type": "COMES_BEFORE", "example_phrase": "入住登记 COMES_BEFORE 领取房卡",
            "description": "", "allow_chain_query": True,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    assert resp.json()["relation_type"] == "COMES_BEFORE"

    resp = client.get(
        "/api/admin/ontology/t1/relation-types?status=draft", headers={"Authorization": "Bearer x"}
    )
    names = {r["relation_type"] for r in resp.json()["relation_types"]}
    assert "COMES_BEFORE" in names
    assert "PRECEDES" not in names


def test_update_tenant_relation_type_route_rejects_name_collision(client):
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.put(
        "/api/admin/ontology/t1/relation-types/PRECEDES",
        json={
            "relation_type": "PART_OF", "example_phrase": "x",
            "description": "", "allow_chain_query": False,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400


def test_migrate_relation_type_route_returns_migrated_count(client):
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient(migrated_count=5)

    resp = client.post(
        "/api/admin/ontology/t1/relation-types/migrate",
        json={"old_type": "PRECEDES", "new_type": "COMES_BEFORE"},
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"migrated_count": 5}


def test_migrate_relation_type_route_maps_value_error_to_400(client):
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient(
        migrate_error=ValueError("旧关系类型名字不合法")
    )

    resp = client.post(
        "/api/admin/ontology/t1/relation-types/migrate",
        json={"old_type": "bad-name", "new_type": "COMES_BEFORE"},
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 400


def test_create_update_delete_product_line(client):
    resp = client.post(
        "/api/admin/ontology/product-lines", json={"value": "核心平台"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.put(
        "/api/admin/ontology/product-lines/核心平台", json={"value": "旗舰平台"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"value": "旗舰平台"}

    resp = client.get("/api/admin/ontology/product-lines", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"product_lines": ["旗舰平台"]}

    resp = client.delete(
        "/api/admin/ontology/product-lines/旗舰平台", headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/product-lines", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"product_lines": []}


def test_delete_product_line_in_use_returns_409(client):
    client.post(
        "/api/admin/ontology/term-types", json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/product-lines", json={"value": "核心平台"},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/terms",
        json={"standard_name": "x", "aliases": [], "term_type": "错误码", "product_line": "核心平台"},
        headers={"Authorization": "Bearer x"},
    )

    resp = client.delete(
        "/api/admin/ontology/product-lines/核心平台", headers={"Authorization": "Bearer x"}
    )

    assert resp.status_code == 409


def test_tenant_ontology_status_flips_after_confirm(client):
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.get("/api/admin/ontology/t1/status", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"confirmed": False}

    resp = client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/status", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"confirmed": True}
