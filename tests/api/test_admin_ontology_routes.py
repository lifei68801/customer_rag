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


@pytest.fixture
def client(monkeypatch):
    conn_holder: dict[str, aiosqlite.Connection] = {}

    async def _get_conn():
        if "conn" not in conn_holder:
            conn_holder["conn"] = await _review_conn()
        return conn_holder["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
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
