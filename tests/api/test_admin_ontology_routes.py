from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
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
    # Task 4：这个文件里的写接口现在会先用 review_conn 调
    # require_active_tenant() 校验 tenant_id——真实的 deps.get_review_conn()
    # 会自动建好 tenants 表并回填历史租户，这里是手工建表的测试连接，绕开了
    # 那条路径，必须显式建表 + 注册本文件全部用例里出现过的 tenant_id（路径
    # 参数 /{tenant_id}/... 里能找到的全部字面量）。
    await create_tenants_table(conn)
    for _tid in ("t1", "muji", "default", "tenant_a", "tenant_b"):
        await create_tenant(conn, tenant_id=_tid, name=_tid)
    return conn


class _FakeGraphClient:
    """占位图谱客户端——本文件里唯一需要真实 Neo4j 写入的场景是
    test_delete_term_type_in_use_returns_409 借道 /api/admin/terms 创建
    一条术语来制造"分类在用"的场景，那条路由依赖 deps.get_graph_client
    做 sync_term()。这里只需要 sync_term 不抛异常，不需要记录调用。

    migrate_relation_type_edges 支持可配置返回值/异常，供迁移路由的测试
    直接构造一个带指定行为的实例覆盖 fixture 里的默认值。"""

    def __init__(
        self, *, migrated_count: int = 0, migrate_error: Exception | None = None,
        ensure_index_error: Exception | None = None,
    ) -> None:
        self._migrated_count = migrated_count
        self._migrate_error = migrate_error
        self._ensure_index_error = ensure_index_error
        self.ensured_index_calls: list[tuple[str, str, list[tuple[str, str]]]] = []

    async def sync_term(self, term) -> None:
        pass

    async def migrate_relation_type_edges(self, *, tenant_id: str, old_type: str, new_type: str) -> int:
        if self._migrate_error is not None:
            raise self._migrate_error
        return self._migrated_count

    async def ensure_extra_field_indexes(self, *, tenant_id, term_type, extra_fields) -> None:
        self.ensured_index_calls.append(
            (tenant_id, term_type, [(f.name, f.value_type) for f in extra_fields])
        )
        if self._ensure_index_error is not None:
            raise self._ensure_index_error


@pytest.fixture
def conn_for_testing() -> dict[str, aiosqlite.Connection]:
    """Holder for connection shared between client fixture and tests."""
    return {}


@pytest.fixture
def client(monkeypatch, conn_for_testing):
    async def _get_conn():
        if "conn" not in conn_for_testing:
            conn_for_testing["conn"] = await _review_conn()
        return conn_for_testing["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_and_list_term_types(client):
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "错误码", "extra_fields": [{"name": "severity_level", "value_type": "string"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    assert resp.json() == {
        "term_types": [
            {
                "value": "错误码",
                "extra_fields": [{"name": "severity_level", "value_type": "string"}],
            }
        ]
    }


def test_create_term_type_with_typed_extra_fields(client):
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={
            "value": "VariantValue",
            "extra_fields": [
                {"name": "numeric_value", "value_type": "number"},
                {"name": "dims", "value_type": "number[]"},
            ],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert resp.json() == {
        "term_types": [
            {
                "value": "VariantValue",
                "extra_fields": [
                    {"name": "numeric_value", "value_type": "number"},
                    {"name": "dims", "value_type": "number[]"},
                ],
            }
        ]
    }


def test_create_term_type_rejects_invalid_extra_field_value_type(client):
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "错误码", "extra_fields": [{"name": "严重等级", "value_type": "不存在的类型"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400


def test_create_term_type_ensures_neo4j_indexes_for_declared_fields(client):
    fake_graph_client = _FakeGraphClient()
    app.dependency_overrides[deps.get_graph_client] = lambda: fake_graph_client

    resp = client.post(
        "/api/admin/ontology/muji/term-types",
        json={
            "value": "Product",
            "extra_fields": [{"name": "numeric_value", "value_type": "number"}],
        },
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 200
    assert fake_graph_client.ensured_index_calls == [
        ("muji", "Product", [("numeric_value", "number")])
    ]


def test_update_term_type_ensures_neo4j_indexes_for_declared_fields(client):
    fake_graph_client = _FakeGraphClient()
    app.dependency_overrides[deps.get_graph_client] = lambda: fake_graph_client
    client.post(
        "/api/admin/ontology/muji/term-types",
        json={"value": "Product", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    fake_graph_client.ensured_index_calls.clear()

    resp = client.put(
        "/api/admin/ontology/muji/term-types/Product",
        json={
            "value": "Product",
            "extra_fields": [{"name": "md_no", "value_type": "string"}],
        },
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 200
    assert fake_graph_client.ensured_index_calls == [
        ("muji", "Product", [("md_no", "string")])
    ]


def test_create_term_type_still_succeeds_when_index_creation_fails(client):
    """Neo4j 索引创建失败不能把已经写成功的 SQLite 声明变成 500——索引只是查询性能
    优化，而客户端对 500 的自然反应（重试）会撞上已存在的记录报 400，把一次可恢复的
    性能降级放大成看起来无解的死循环。"""
    fake_graph_client = _FakeGraphClient(ensure_index_error=RuntimeError("neo4j unreachable"))
    app.dependency_overrides[deps.get_graph_client] = lambda: fake_graph_client

    resp = client.post(
        "/api/admin/ontology/muji/term-types",
        json={
            "value": "Product",
            "extra_fields": [{"name": "numeric_value", "value_type": "number"}],
        },
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 200
    assert fake_graph_client.ensured_index_calls == [("muji", "Product", [("numeric_value", "number")])]

    # 声明本身确实落库了（不是靠跳过写入换来的 200）
    resp = client.get("/api/admin/ontology/muji/term-types", headers={"Authorization": "Bearer x"})
    assert [t["value"] for t in resp.json()["term_types"]] == ["Product"]


def test_update_term_type_still_succeeds_when_index_creation_fails(client):
    fake_graph_client = _FakeGraphClient()
    app.dependency_overrides[deps.get_graph_client] = lambda: fake_graph_client
    client.post(
        "/api/admin/ontology/muji/term-types",
        json={"value": "Product", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    failing_graph_client = _FakeGraphClient(ensure_index_error=RuntimeError("neo4j unreachable"))
    app.dependency_overrides[deps.get_graph_client] = lambda: failing_graph_client

    resp = client.put(
        "/api/admin/ontology/muji/term-types/Product",
        json={
            "value": "Product",
            "extra_fields": [{"name": "md_no", "value_type": "string"}],
        },
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 200
    assert failing_graph_client.ensured_index_calls == [("muji", "Product", [("md_no", "string")])]


async def test_delete_term_type_in_use_returns_409(client, conn_for_testing):
    # Fixture setup: create term_type and product_line categories
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/product-lines", json={"value": "示例产品线"},
        headers={"Authorization": "Bearer x"},
    )

    # Directly insert a term into the database (admin_terms_routes.py is Task 4's responsibility,
    # so we bypass it by inserting directly). This establishes the "term_type in use" condition.
    # Use the same connection that the client fixture is using, not a new one.
    await conn_for_testing["conn"].execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, product_line, extra_properties) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("default", "x", "x", "[]", "错误码", "示例产品线", "{}"),
    )
    await conn_for_testing["conn"].commit()

    resp = client.delete(
        "/api/admin/ontology/default/term-types/错误码", headers={"Authorization": "Bearer x"}
    )

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
    # term-types 和 constraints 都用 "default" 租户，保证约束校验能看到同一批分类。
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "客房", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "酒店", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/default/checkout", headers={"Authorization": "Bearer x"})

    resp = client.post(
        "/api/admin/ontology/default/constraints",
        json={"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/default/constraints?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert resp.json()["constraints"] == [
        {"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"}
    ]


def test_remove_constraint_via_delete_with_body(client):
    """DELETE /{tenant_id}/constraints 带 body——确认 TestClient 真的能把
    body 发送到一个 DELETE 请求上，路由端能正常解析。
    term-types 和 constraints 都用 "default" 租户，保证约束校验能看到同一批分类。
    """
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "客房", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "酒店", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/default/checkout", headers={"Authorization": "Bearer x"})
    client.post(
        "/api/admin/ontology/default/constraints",
        json={"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"},
        headers={"Authorization": "Bearer x"},
    )

    resp = client.request(
        "DELETE", "/api/admin/ontology/default/constraints",
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


async def test_delete_product_line_in_use_returns_409(client, conn_for_testing):
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/product-lines", json={"value": "核心平台"},
        headers={"Authorization": "Bearer x"},
    )

    # Directly insert a term into the database (admin_terms_routes.py is Task 4's responsibility,
    # so we bypass it by inserting directly). This establishes the "product_line in use" condition.
    # Use the same connection that the client fixture is using, not a new one.
    await conn_for_testing["conn"].execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, product_line, extra_properties) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("default", "x", "x", "[]", "错误码", "核心平台", "{}"),
    )
    await conn_for_testing["conn"].commit()

    resp = client.delete(
        "/api/admin/ontology/product-lines/核心平台", headers={"Authorization": "Bearer x"}
    )

    assert resp.status_code == 409


def test_term_type_routes_are_scoped_to_tenant_in_url(client):
    resp = client.post(
        "/api/admin/ontology/tenant_a/term-types",
        json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/tenant_b/term-types", headers={"Authorization": "Bearer x"}
    )
    assert resp.json() == {"term_types": []}


def test_create_term_type_category_returns_404_for_unknown_tenant(client):
    """Task 4：写接口在具体业务逻辑之前要先校验 tenant_id 在 tenants 注册表
    里存在且是 active——一个从未注册过的 tenant_id 应该直接 404。"""
    resp = client.post(
        "/api/admin/ontology/no-such-tenant/term-types",
        json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 404


def test_migrate_relation_type_route_returns_404_for_unknown_tenant_and_succeeds_for_known_tenant(
    client,
):
    """migrate_tenant_relation_type 是本文件唯一一个新增了 review_conn 依赖的
    路由（它原本只连 Neo4j，不碰 SQLite）——这里既验证新加的租户校验对未知
    租户生效（404），也验证新加的依赖没有破坏这个路由本身对已知/active
    租户的正常工作（沿用 test_migrate_relation_type_route_returns_migrated_count
    的正面用例，确认没有回归）。"""
    resp = client.post(
        "/api/admin/ontology/no-such-tenant/relation-types/migrate",
        json={"old_type": "PRECEDES", "new_type": "COMES_BEFORE"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 404

    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient(migrated_count=3)
    resp = client.post(
        "/api/admin/ontology/t1/relation-types/migrate",
        json={"old_type": "PRECEDES", "new_type": "COMES_BEFORE"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"migrated_count": 3}


def test_tenant_ontology_status_flips_after_confirm(client):
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.get("/api/admin/ontology/t1/status", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"confirmed": False}

    resp = client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/status", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"confirmed": True}
