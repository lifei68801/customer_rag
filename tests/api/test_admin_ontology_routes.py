from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.ontology_change_log import list_ontology_changes
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app


def _fake_admin_session() -> AdminSession:
    """跳过鉴权用的假身份。

    以前这里是 `lambda: None`——那时 require_admin_session 的返回值没人用。
    现在 require_tenant_access 要读它的 role/tenant_id，返回 None 会让每条
    请求都撞上 AttributeError。用 admin 身份：这些测试关心的是路由逻辑，
    不是权限，admin 能进任意租户。
    """
    return AdminSession(username="admin", role="admin", tenant_id=None, expires_at=1e18)



pytestmark = pytest.mark.anyio


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    # ensure_ontology_schema 只建分类/关系类型/约束三张表——这个 fixture 还需要
    # terms 表存在，因为 test_delete_term_type_in_use_returns_409 要通过
    # /api/admin/terms 创建一条术语来制造"分类在用"的场景（terms 表由 Task 6 的
    # terms_store.py 管理，不在 ontology_lifecycle 的统一建表入口里）。
    await ensure_terms_schema(conn)
    # delete_term_type 的 terms 引用检查走合并视图（terms 叠加 term_edits），
    # 要读 term_edits 表。真实的 open_ontology_store_conn 会建它，这个手工
    # 建表的连接必须显式补上。
    await ensure_term_edits_schema(conn)
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
        ensure_index_error: Exception | None = None, term_type_migrated_count: int = 0,
        term_type_migrate_error: Exception | None = None,
    ) -> None:
        self._migrated_count = migrated_count
        self._migrate_error = migrate_error
        self._ensure_index_error = ensure_index_error
        self._term_type_migrated_count = term_type_migrated_count
        self._term_type_migrate_error = term_type_migrate_error
        self.ensured_index_calls: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.migrate_term_type_nodes_calls: list[tuple[str, str, str]] = []
        self.fanout_by_relation: dict[str, int] = {}
        self.fanout_error: Exception | None = None
        # Task 7：确认本体前的存量日期值扫描。non_iso_by_field 按字段名配置
        # 返回值（默认 (0, []) 即"干净"）；date_scan_calls 记录实际发生过的
        # 扫描调用，供"不该扫的字段没被扫"这类用例断言——只断言"确认成功"
        # 在"每次都扫、但恰好没有脏值"的实现下也是绿的，测不出该测的东西。
        self.non_iso_by_field: dict[str, tuple[int, list[str]]] = {}
        self.date_scan_calls: list[tuple[str, str]] = []

    async def sync_term(self, term) -> None:
        pass

    async def count_non_iso_date_values(
        self, *, tenant_id: str, term_type: str, field: str
    ) -> tuple[int, list[str]]:
        self.date_scan_calls.append((term_type, field))
        return self.non_iso_by_field.get(field, (0, []))

    async def probe_relation_fanout(
        self, *, tenant_id: str, relation_type: str, from_term_type: str,
        to_term_type: str, direction: str,
    ) -> int:
        # 默认 1（函数关系，无扇出）；要测扇出的用例把 fanout_by_relation
        # 设成 {关系类型: 扇出度}，要测探测失败的设 fanout_error。
        if self.fanout_error is not None:
            raise self.fanout_error
        return self.fanout_by_relation.get(relation_type, 1)

    async def migrate_relation_type_edges(self, *, tenant_id: str, old_type: str, new_type: str) -> int:
        if self._migrate_error is not None:
            raise self._migrate_error
        return self._migrated_count

    async def migrate_term_type_nodes(self, *, tenant_id: str, old_type: str, new_type: str) -> int:
        self.migrate_term_type_nodes_calls.append((tenant_id, old_type, new_type))
        if self._term_type_migrate_error is not None:
            raise self._term_type_migrate_error
        return self._term_type_migrated_count

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
def client_as_alice(monkeypatch, conn_for_testing):
    """跟 client 一样，只是登录身份是 alice 而不是 admin。

    变更日志那组用例必须用这个：_fake_admin_session 的用户名是 "admin"，
    而"写死一个 admin"正是这次要防的那种实现，用 admin 断言 actor ==
    "admin" 的话两种实现都能过。"""

    async def _get_conn():
        if "conn" not in conn_for_testing:
            conn_for_testing["conn"] = await _review_conn()
        return conn_for_testing["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: AdminSession(
        username="alice", role="admin", tenant_id=None, expires_at=1e18
    )
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client(monkeypatch, conn_for_testing):
    async def _get_conn():
        if "conn" not in conn_for_testing:
            conn_for_testing["conn"] = await _review_conn()
        return conn_for_testing["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    app.dependency_overrides[deps.require_admin_session] = _fake_admin_session
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
                "extra_fields": [
                    {"name": "severity_level", "value_type": "string", "label": ""}
                ],
                "standard_name_value_type": "string",
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
                    {"name": "numeric_value", "value_type": "number", "label": ""},
                    {"name": "dims", "value_type": "number[]", "label": ""},
                ],
                "standard_name_value_type": "string",
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
    # Fixture setup: create term_type category
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )

    # Directly insert a term into the database (admin_terms_routes.py is Task 4's responsibility,
    # so we bypass it by inserting directly). This establishes the "term_type in use" condition.
    # Use the same connection that the client fixture is using, not a new one.
    await conn_for_testing["conn"].execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("default", "x", "x", "[]", "错误码", "{}"),
    )
    await conn_for_testing["conn"].commit()

    resp = client.delete(
        "/api/admin/ontology/default/term-types/错误码", headers={"Authorization": "Bearer x"}
    )

    assert resp.status_code == 409


async def test_delete_term_type_in_use_409_body_carries_blocking_terms(client, conn_for_testing):
    """409 的响应体除了人话，还要带结构化的挡路术语——前端要据此生成"去
    实体列表里筛出它们"的链接。只给一句话，用户看得见却纠正不了。"""
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "module", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    await conn_for_testing["conn"].execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("default", "示例登录模块", "示例登录模块", "[]", "module", "{}"),
    )
    await conn_for_testing["conn"].commit()

    resp = client.delete(
        "/api/admin/ontology/default/term-types/module", headers={"Authorization": "Bearer x"}
    )

    assert resp.status_code == 409
    body = resp.json()
    # detail 仍是一句可读的人话（既有前端直接展示它），并且点名到具体是谁。
    assert isinstance(body["detail"], str)
    assert "示例登录模块" in body["detail"]
    assert body["blocking_terms"] == {
        "term_type": "module",
        "total": 1,
        "node_keys": ["示例登录模块"],
    }
    assert body["blocking_constraints_total"] == 0


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


def test_list_term_types_filters_by_status_and_defaults_to_draft(client):
    """Task 4：草稿创建的 term_type 只在 status=draft 的查询里出现；确认之后
    (草稿行原地改成 confirmed) 只在 status=confirmed 的查询里出现，draft 那边
    应该清空。不传 status 应该等价于 status=draft（新增参数的默认值）。"""
    client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert [t["value"] for t in resp.json()["term_types"]] == ["错误码"]

    resp = client.get(
        "/api/admin/ontology/t1/term-types?status=confirmed", headers={"Authorization": "Bearer x"}
    )
    assert resp.json() == {"term_types": []}

    resp = client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/term-types?status=confirmed", headers={"Authorization": "Bearer x"}
    )
    assert [t["value"] for t in resp.json()["term_types"]] == ["错误码"]

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"term_types": []}


async def test_migrate_term_type_route_migrates_terms_and_graph_nodes(client, conn_for_testing):
    """创建草稿类型 -> 确认 -> 写几条引用该类型的 terms 行 -> 调用迁移接口 ->
    terms_migrated 反映 SQLite 侧真实受影响的行数，graph_nodes_migrated 反映
    fake 图谱客户端配置的返回值，两者互相独立。"""
    client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})

    conn = conn_for_testing["conn"]
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
        ("t1", "k1", "网关超时", "[]", "错误码", "{}"),
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
        ("t1", "k2", "登录失败", "[]", "错误码", "{}"),
    )
    await conn.commit()

    fake_graph_client = _FakeGraphClient(term_type_migrated_count=7)
    app.dependency_overrides[deps.get_graph_client] = lambda: fake_graph_client

    resp = client.post(
        "/api/admin/ontology/t1/term-types/migrate",
        json={"old_type": "错误码", "new_type": "故障代码"},
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"terms_migrated": 2, "graph_nodes_migrated": 7}
    assert fake_graph_client.migrate_term_type_nodes_calls == [("t1", "错误码", "故障代码")]


async def test_migrate_term_type_route_reports_terms_migrated_when_graph_sync_fails(
    client, conn_for_testing,
):
    """SQLite 侧的 migrate_term_type 已经 commit 过了——如果紧接着的 Neo4j
    调用失败，接口不能返回裸的 500，得让操作员知道 SQLite 那边已经迁移
    成功、只需要重试（幂等的）Neo4j 那一半。"""
    client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})

    conn = conn_for_testing["conn"]
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
        ("t1", "k1", "网关超时", "[]", "错误码", "{}"),
    )
    await conn.commit()

    fake_graph_client = _FakeGraphClient(
        term_type_migrate_error=RuntimeError("Neo4j 连接超时")
    )
    app.dependency_overrides[deps.get_graph_client] = lambda: fake_graph_client

    resp = client.post(
        "/api/admin/ontology/t1/term-types/migrate",
        json={"old_type": "错误码", "new_type": "故障代码"},
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 502
    assert "terms_migrated=1" in resp.json()["detail"]


def test_migrate_term_type_route_returns_404_for_unknown_tenant(client):
    resp = client.post(
        "/api/admin/ontology/no-such-tenant/term-types/migrate",
        json={"old_type": "错误码", "new_type": "故障代码"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 404


def test_tenant_ontology_status_flips_after_confirm(client):
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.get("/api/admin/ontology/t1/status", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"confirmed": False}

    resp = client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/status", headers={"Authorization": "Bearer x"})
    assert resp.json() == {"confirmed": True}


def test_create_term_type_accepts_standard_name_value_type(client):
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "销量", "extra_fields": [], "standard_name_value_type": "number"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    assert resp.json()["standard_name_value_type"] == "number"

    listing = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert listing.json()["term_types"][0]["standard_name_value_type"] == "number"


def test_create_term_type_without_standard_name_value_type_defaults_to_string(client):
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "产品", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.json()["standard_name_value_type"] == "string"


def test_update_term_type_rejects_invalid_standard_name_value_type(client):
    client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "销量", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    resp = client.put(
        "/api/admin/ontology/t1/term-types/销量",
        json={"value": "销量", "extra_fields": [], "standard_name_value_type": "not-a-type"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400


def test_graph_overlay_reports_real_data_fanout_and_entity_counts(client):
    """约束表只说明「这个组合被允许」，说不出实际数据里一个主语节点会连到
    几个宾语节点——而后者才是扇形陷阱的判据。本体层看不出这件事：本体只
    声明了一条边，是不是一对多要问图谱。"""
    for value in ("产品", "公司"):
        client.post(
            f"/api/admin/ontology/default/term-types", json={"value": value, "extra_fields": []},
            headers={"Authorization": "Bearer x"},
        )
    client.post("/api/admin/ontology/default/checkout", headers={"Authorization": "Bearer x"})
    # SOLD_BY 不在默认播种的 10 种通用关系里，约束校验要求它先存在于草稿。
    client.post(
        "/api/admin/ontology/default/relation-types",
        json={"relation_type": "SOLD_BY", "example_phrase": "产品 SOLD_BY 公司"},
        headers={"Authorization": "Bearer x"},
    )
    added = client.post(
        "/api/admin/ontology/default/constraints",
        json={"subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "公司"},
        headers={"Authorization": "Bearer x"},
    )
    assert added.status_code == 200, added.text

    fake = _FakeGraphClient()
    fake.fanout_by_relation = {"SOLD_BY": 3}
    app.dependency_overrides[deps.get_graph_client] = lambda: fake
    try:
        resp = client.get(
            "/api/admin/ontology/default/graph-overlay?status=draft",
            headers={"Authorization": "Bearer x"},
        )
    finally:
        app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()

    assert resp.status_code == 200
    body = resp.json()
    assert body["fanout"] == [
        {
            "subject_term_type": "产品",
            "relation_type": "SOLD_BY",
            "object_term_type": "公司",
            "fanout": 3,
        }
    ]
    # 实体计数跟扇出一起返回——两者都只服务本体图，分两个接口只是多一次往返。
    # 这个租户还没有任何实体，所以是空字典而不是缺字段。
    assert body["entity_counts"] == {}


def test_graph_overlay_degrades_fanout_to_null_when_probe_fails(client):
    """单条探测失败不该让整个视图报错——图谱可能正在重建、某个类型还没有
    任何节点。退回「未知」（null）而不是 500。"""
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "产品", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/default/term-types", json={"value": "公司", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/default/checkout", headers={"Authorization": "Bearer x"})
    # SOLD_BY 不在默认播种的 10 种通用关系里，约束校验要求它先存在于草稿。
    client.post(
        "/api/admin/ontology/default/relation-types",
        json={"relation_type": "SOLD_BY", "example_phrase": "产品 SOLD_BY 公司"},
        headers={"Authorization": "Bearer x"},
    )
    added = client.post(
        "/api/admin/ontology/default/constraints",
        json={"subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "公司"},
        headers={"Authorization": "Bearer x"},
    )
    assert added.status_code == 200, added.text

    fake = _FakeGraphClient()
    fake.fanout_error = RuntimeError("图谱不可用")
    app.dependency_overrides[deps.get_graph_client] = lambda: fake
    try:
        resp = client.get(
            "/api/admin/ontology/default/graph-overlay?status=draft",
            headers={"Authorization": "Bearer x"},
        )
    finally:
        app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()

    assert resp.status_code == 200
    assert resp.json()["fanout"][0]["fanout"] is None


def test_replace_draft_writes_the_whole_ontology(client):
    """一次请求写入整套本体。"""
    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"},
                {"value": "产品", "extra_fields": [], "standard_name_value_type": "string"},
            ],
            "relation_types": [
                {"relation_type": "CONTAINS", "example_phrase": "订单 CONTAINS 产品"}
            ],
            "constraints": [
                {"subject_term_type": "订单号", "relation_type": "CONTAINS", "object_term_type": "产品"}
            ],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 200

    listed = client.get(
        "/api/admin/ontology/t1/term-types?status=draft", headers={"Authorization": "Bearer x"}
    ).json()
    assert {t["value"] for t in listed["term_types"]} == {"订单号", "产品"}


def test_replace_draft_rejects_constraint_referencing_undeclared_type(client):
    """引用未声明的类型必须 400，不能静静写进去。

    写进去的话，ETL 跑批时才会炸——那时用户已经在等结果了，而错误信息
    指向的是 ETL，不是本体。
    """
    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"}
            ],
            "relation_types": [{"relation_type": "CONTAINS", "example_phrase": "订单 CONTAINS 产品"}],
            "constraints": [
                {"subject_term_type": "订单号", "relation_type": "CONTAINS", "object_term_type": "幽灵"}
            ],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 400


def test_replace_draft_rejects_duplicate_term_type_in_same_submission(client):
    """同一份提交里出现两个同名 term_type，现在会撞 (tenant_id, value, status)
    这个主键——写入阶段查出来就是裸 500，校验阶段查出来才能是可读的 400。

    这条用例还断言草稿在拒绝后保持原样：先用一次成功提交建立一份已知草稿，
    再提交一份带重复项的草案，确认失败后旧草稿没有被动过（校验先于任何
    DELETE/INSERT，所以这个"未改动"是自动满足的，但值得显式断言防回归）。
    """
    setup = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"}
            ],
            "relation_types": [],
            "constraints": [],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert setup.status_code == 200

    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "重复类型", "extra_fields": [], "standard_name_value_type": "string"},
                {"value": "重复类型", "extra_fields": [], "standard_name_value_type": "string"},
            ],
            "relation_types": [],
            "constraints": [],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 400

    listed = client.get(
        "/api/admin/ontology/t1/term-types?status=draft", headers={"Authorization": "Bearer x"}
    ).json()
    assert {t["value"] for t in listed["term_types"]} == {"订单号"}, "失败的提交不该改动既有草稿"


def test_replace_draft_stores_etl_mapping(client):
    """引导一次提交同时写本体草稿和映射。

    分两次请求写不行：中途失败会留下一份没有映射的草稿，而用户不知道
    映射没写进去——他在表格导入页会看到"从头配置"的界面。
    """
    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [{"value": "客户", "extra_fields": []}],
            "relation_types": [],
            "constraints": [],
            "etl_mapping": {
                "config_yaml": "entities: []",
                "source_file_name": "orders.csv",
            },
        },
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 200

    got = client.get(
        "/api/admin/ontology/t1/etl-mapping?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert got.status_code == 200
    assert got.json()["mapping"]["source_file_name"] == "orders.csv"


def test_replace_draft_without_mapping_leaves_it_absent(client):
    """etl_mapping 是可选的：不带映射的提交不能凭空造一份出来。

    这是"从没写过映射"的基线——租户此前既没有 draft 也没有 confirmed 映射，
    一次不带映射的整份替换之后，draft 侧仍然应该是"没有映射"。
    """
    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={"term_types": [], "relation_types": [], "constraints": []},
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 200
    got = client.get(
        "/api/admin/ontology/t1/etl-mapping?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert got.json()["mapping"] is None


def test_replace_draft_without_mapping_does_not_erase_existing_mapping(client):
    """不带映射的提交不能把 draft 侧已有的映射抹掉。

    这条只覆盖 draft 侧：两次提交之间没有 confirm，第二次提交看到的
    draft 行还是第一次写的那份。中间隔着一次 confirm 的那条路是另一回事，
    也是真正会丢数据的那条，见
    test_replace_draft_without_mapping_keeps_confirmed_mapping_across_next_confirm。

    （这里以前写着"本体结构页那三个 tab 改草稿时不带 etl_mapping"。不成立：
    那三个 tab 走的是 /checkout 加逐条的 term-types / relation-types /
    constraints 端点，全仓库 /draft/replace 只有 GuidedOntologyPage 一个
    调用方。）
    """
    with_mapping = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [{"value": "客户", "extra_fields": []}],
            "relation_types": [],
            "constraints": [],
            "etl_mapping": {
                "config_yaml": "entities: []",
                "source_file_name": "orders.csv",
            },
        },
        headers={"Authorization": "Bearer x"},
    )
    assert with_mapping.status_code == 200

    without_mapping = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [{"value": "客户", "extra_fields": []}],
            "relation_types": [],
            "constraints": [],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert without_mapping.status_code == 200

    got = client.get(
        "/api/admin/ontology/t1/etl-mapping?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert got.json()["mapping"]["source_file_name"] == "orders.csv"
    assert got.json()["mapping"]["config_yaml"] == "entities: []"


def test_replace_draft_without_mapping_keeps_confirmed_mapping_across_next_confirm(client):
    """已确认的映射不能因为一次不带映射的整份替换而消失。

    上一条只走到 draft 侧：那两次提交之间没有 confirm，第二次 replace_draft
    看到的 draft 行还是第一次写的那份，"原样还在"是自动成立的。真正会丢
    数据的是中间有一次 confirm 的这条路：

        replace_draft(带映射) → confirm     # confirmed 映射 = orders.csv
        replace_draft(不带映射)             # 写三张草稿表 + 写 checkout 标记
        checkout                            # 标记已在 → 早退 → 不复制映射
        confirm                             # 删 confirmed 映射 + 提升空 draft

    终点是 get_etl_mapping(status='confirmed') is None，全程 200、没有任何
    提示：用户下次进表格导入页，界面从"引导流程已为这个本体配好映射"变回
    "把这张表映射到已有本体"，他不知道为什么，也无从恢复。
    """
    with_mapping = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [{"value": "客户", "extra_fields": []}],
            "relation_types": [],
            "constraints": [],
            "etl_mapping": {
                "config_yaml": "entities: []",
                "source_file_name": "orders.csv",
            },
        },
        headers={"Authorization": "Bearer x"},
    )
    assert with_mapping.status_code == 200
    assert (
        client.post(
            "/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"}
        ).status_code
        == 200
    )
    confirmed = client.get(
        "/api/admin/ontology/t1/etl-mapping?status=confirmed",
        headers={"Authorization": "Bearer x"},
    )
    # 前置条件，不是本条要测的东西：确认这条路的起点确实有一份已确认映射，
    # 否则后面那句"映射还在"会在"从来就没有过映射"的情况下也绿。
    assert confirmed.json()["mapping"]["source_file_name"] == "orders.csv"

    without_mapping = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [{"value": "客户", "extra_fields": []}],
            "relation_types": [],
            "constraints": [],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert without_mapping.status_code == 200
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})
    assert (
        client.post(
            "/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"}
        ).status_code
        == 200
    )

    got = client.get(
        "/api/admin/ontology/t1/etl-mapping?status=confirmed",
        headers={"Authorization": "Bearer x"},
    )
    assert got.json()["mapping"] is not None, "第二次确认之后已确认映射不见了"
    assert got.json()["mapping"]["source_file_name"] == "orders.csv"


def test_term_type_extra_field_label_round_trips_through_api(client):
    """属性的显示名要能存进去、读出来，且跟内部名是两个独立的值。"""
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={
            "value": "商品",
            "extra_fields": [{"name": "price", "value_type": "number", "label": "售价"}],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert resp.json()["term_types"][0]["extra_fields"] == [
        {"name": "price", "value_type": "number", "label": "售价"}
    ]


def test_term_type_extra_field_label_defaults_to_empty_when_not_given(client):
    """不带 label 的旧调用方照常工作，读回来 label 是空串——前端据此回退到
    内部名显示。后端不替调用方猜一个显示名。"""
    resp = client.post(
        "/api/admin/ontology/t1/term-types",
        json={"value": "商品", "extra_fields": [{"name": "price", "value_type": "number"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert resp.json()["term_types"][0]["extra_fields"] == [
        {"name": "price", "value_type": "number", "label": ""}
    ]


def test_replace_draft_keeps_extra_field_label(client):
    resp = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {
                    "value": "商品",
                    "extra_fields": [
                        {"name": "price", "value_type": "number", "label": "售价"}
                    ],
                    "standard_name_value_type": "string",
                }
            ],
            "relation_types": [],
            "constraints": [],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/t1/term-types", headers={"Authorization": "Bearer x"})
    assert resp.json()["term_types"][0]["extra_fields"] == [
        {"name": "price", "value_type": "number", "label": "售价"}
    ]


# ---------------------------------------------------------------------------
# 本体变更日志：API 层把登录会话里的用户名传下去
#
# 这组用例登录身份是 alice（client_as_alice），不是 admin——见那个 fixture
# 的说明。
# ---------------------------------------------------------------------------


def _changes(conn_for_testing, tenant_id: str = "t1"):
    async def _run():
        return await list_ontology_changes(conn_for_testing["conn"], tenant_id)

    return asyncio.run(_run())


def _headers() -> dict:
    return {"Authorization": "Bearer x"}


def test_creating_a_term_type_logs_the_logged_in_user(client_as_alice, conn_for_testing):
    resp = client_as_alice.post(
        "/api/admin/ontology/t1/term-types", json={"value": "错误码"}, headers=_headers()
    )

    assert resp.status_code == 200, resp.text
    change = _changes(conn_for_testing)[-1]
    assert change.actor == "alice"
    assert change.action == "create"
    assert change.object_kind == "term_type"
    assert change.object_id == "错误码"


def test_deleting_a_term_type_logs_the_logged_in_user(client_as_alice, conn_for_testing):
    client_as_alice.post(
        "/api/admin/ontology/t1/term-types", json={"value": "错误码"}, headers=_headers()
    )

    resp = client_as_alice.delete(
        "/api/admin/ontology/t1/term-types/错误码", headers=_headers()
    )

    assert resp.status_code == 200, resp.text
    change = _changes(conn_for_testing)[-1]
    assert change.actor == "alice"
    assert change.action == "delete"
    assert change.object_kind == "term_type"
    assert change.object_id == "错误码"


def test_relation_type_writes_log_the_logged_in_user(client_as_alice, conn_for_testing):
    client_as_alice.post(
        "/api/admin/ontology/t1/relation-types",
        json={"relation_type": "SOLD_BY", "example_phrase": "产品 SOLD_BY 公司"},
        headers=_headers(),
    )
    client_as_alice.put(
        "/api/admin/ontology/t1/relation-types/SOLD_BY",
        json={"relation_type": "SUPPLIED_BY", "example_phrase": "产品 SUPPLIED_BY 公司"},
        headers=_headers(),
    )
    client_as_alice.delete(
        "/api/admin/ontology/t1/relation-types/SUPPLIED_BY", headers=_headers()
    )

    changes = _changes(conn_for_testing)
    assert [(c.actor, c.action, c.object_id) for c in changes] == [
        ("alice", "create", "SOLD_BY"),
        ("alice", "update", "SOLD_BY"),
        ("alice", "delete", "SUPPLIED_BY"),
    ]


def test_constraint_writes_log_the_logged_in_user(client_as_alice, conn_for_testing):
    client_as_alice.post(
        "/api/admin/ontology/t1/term-types", json={"value": "产品"}, headers=_headers()
    )
    client_as_alice.post(
        "/api/admin/ontology/t1/term-types", json={"value": "公司"}, headers=_headers()
    )
    client_as_alice.post(
        "/api/admin/ontology/t1/relation-types",
        json={"relation_type": "SOLD_BY", "example_phrase": "产品 SOLD_BY 公司"},
        headers=_headers(),
    )
    body = {
        "subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "公司",
    }
    client_as_alice.post(
        "/api/admin/ontology/t1/constraints", json=body, headers=_headers()
    )
    client_as_alice.request(
        "DELETE", "/api/admin/ontology/t1/constraints", json=body, headers=_headers()
    )

    changes = [c for c in _changes(conn_for_testing) if c.object_kind == "constraint"]
    assert [(c.actor, c.action, c.object_id) for c in changes] == [
        ("alice", "create", "产品 -SOLD_BY-> 公司"),
        ("alice", "delete", "产品 -SOLD_BY-> 公司"),
    ]


def test_confirming_the_ontology_logs_the_logged_in_user(client_as_alice, conn_for_testing):
    client_as_alice.post(
        "/api/admin/ontology/t1/term-types", json={"value": "错误码"}, headers=_headers()
    )

    resp = client_as_alice.post("/api/admin/ontology/t1/confirm", headers=_headers())

    assert resp.status_code == 200, resp.text
    change = _changes(conn_for_testing)[-1]
    assert change.actor == "alice"
    assert change.action == "confirm"
    assert change.object_kind == "ontology"
    assert change.details["term_types"] == 1


def test_replacing_the_draft_logs_the_logged_in_user(client_as_alice, conn_for_testing):
    resp = client_as_alice.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [{"value": "产品"}, {"value": "公司"}],
            "relation_types": [
                {"relation_type": "SOLD_BY", "example_phrase": "产品 SOLD_BY 公司"}
            ],
            "constraints": [
                {
                    "subject_term_type": "产品",
                    "relation_type": "SOLD_BY",
                    "object_term_type": "公司",
                }
            ],
        },
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    change = _changes(conn_for_testing)[-1]
    assert change.actor == "alice"
    assert change.action == "replace"
    assert change.object_kind == "ontology_draft"
    assert change.details["term_type_values"] == ["产品", "公司"]


# ---------------------------------------------------------------------------
# 批量删除：本体结构页三张表（实体类型 / 关系类型 / 关系约束）
#
# 这三张表都没有分页也没有筛选，所以只有"选中的这些"一种模式，没有实体
# 明细页那种"筛选条件下的全部"。共用的是 run_bulk_delete 的执行语义：
# 能删的删掉、挡住的逐条报出来、不整批回滚。
#
# 这组用例的每个批次都**同时**放了能删的和删不掉的。全能删或全删不掉的
# 批次是假绿：那样"逐条收集失败"和"一律成功/一律失败"两种实现都能过。
# ---------------------------------------------------------------------------


def _bulk_delete_setup(client, tenant: str = "t1") -> None:
    """一份三类都有的草稿：两个空闲类型、被约束占住的两个类型、一条关系、一条约束。"""
    for value in ("空闲甲", "空闲乙", "产品", "公司"):
        client.post(
            f"/api/admin/ontology/{tenant}/term-types", json={"value": value}, headers=_headers()
        )
    client.post(
        f"/api/admin/ontology/{tenant}/relation-types",
        json={"relation_type": "SOLD_BY", "example_phrase": "产品 SOLD_BY 公司"},
        headers=_headers(),
    )
    client.post(
        f"/api/admin/ontology/{tenant}/constraints",
        json={
            "subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "公司",
        },
        headers=_headers(),
    )


def _term_type_values(client, tenant: str = "t1") -> list[str]:
    resp = client.get(f"/api/admin/ontology/{tenant}/term-types", headers=_headers())
    return [t["value"] for t in resp.json()["term_types"]]


async def test_bulk_delete_term_types_deletes_what_it_can_and_names_each_blocker(
    client_as_alice, conn_for_testing
):
    """批次里有能删的、有被术语挡住的、有被草稿约束挡住的、有根本不存在的。

    四种混在一起是刻意的：只放能删的那种，"一律成功"的实现也能过。
    """
    _bulk_delete_setup(client_as_alice)
    client_as_alice.post(
        "/api/admin/ontology/t1/term-types", json={"value": "module"}, headers=_headers()
    )
    await conn_for_testing["conn"].execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("t1", "示例登录模块", "示例登录模块", "[]", "module", "{}"),
    )
    await conn_for_testing["conn"].commit()

    resp = client_as_alice.post(
        "/api/admin/ontology/t1/term-types/bulk-delete",
        json={"values": ["空闲甲", "module", "产品", "查无此类"]},
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested"] == 4
    assert body["deleted"] == 1
    failures = {f["key"]: f["reason"] for f in body["failures"]}
    assert set(failures) == {"module", "产品", "查无此类"}
    # 沿用单条删除的报错原文：它点名了挡路的是谁，退化成"删除失败"用户就
    # 只能自己去翻。
    assert "示例登录模块" in failures["module"]
    assert "产品 -SOLD_BY-> 公司" in failures["产品"]
    assert "不存在" in failures["查无此类"]
    # 失败的那几条确实还在——只断言 failures 非空的话，"报了失败但照删不误"
    # 也能过。
    remaining = _term_type_values(client_as_alice)
    assert "空闲甲" not in remaining
    assert "module" in remaining
    assert "产品" in remaining


async def test_bulk_delete_term_types_logs_only_the_ones_it_actually_deleted(
    client_as_alice, conn_for_testing
):
    _bulk_delete_setup(client_as_alice)
    client_as_alice.post(
        "/api/admin/ontology/t1/term-types", json={"value": "module"}, headers=_headers()
    )
    await conn_for_testing["conn"].execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, extra_properties) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("t1", "示例登录模块", "示例登录模块", "[]", "module", "{}"),
    )
    await conn_for_testing["conn"].commit()

    resp = client_as_alice.post(
        "/api/admin/ontology/t1/term-types/bulk-delete",
        json={"values": ["空闲甲", "空闲乙", "module", "查无此类"]},
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text

    # 直接 await 而不是走 _changes()：那个辅助函数内部是 asyncio.run，在
    # async 用例里会撞上"已经有事件循环在跑"。
    changes = await list_ontology_changes(conn_for_testing["conn"], "t1")
    deletions = [
        (c.actor, c.object_id)
        for c in changes
        if c.action == "delete" and c.object_kind == "term_type"
    ]
    # 挡住的和不存在的没有发生变更，不该在日志里留痕；actor 是登录的 alice
    # 而不是写死的 admin。
    assert deletions == [("alice", "空闲甲"), ("alice", "空闲乙")]


def test_bulk_delete_relation_types_deletes_what_it_can_and_names_each_failure(client_as_alice):
    _bulk_delete_setup(client_as_alice)
    client_as_alice.post(
        "/api/admin/ontology/t1/relation-types",
        json={"relation_type": "PART_OF", "example_phrase": "模块 PART_OF 产品"},
        headers=_headers(),
    )

    resp = client_as_alice.post(
        "/api/admin/ontology/t1/relation-types/bulk-delete",
        json={"relation_types": ["PART_OF", "查无此关系"]},
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["requested"], body["deleted"]) == (2, 1)
    assert [(f["key"], "不存在" in f["reason"]) for f in body["failures"]] == [("查无此关系", True)]
    listed = [
        r["relation_type"]
        for r in client_as_alice.get(
            "/api/admin/ontology/t1/relation-types", headers=_headers()
        ).json()["relation_types"]
    ]
    assert "PART_OF" not in listed
    assert "SOLD_BY" in listed


def test_bulk_delete_relation_types_logs_only_the_ones_it_actually_deleted(
    client_as_alice, conn_for_testing
):
    _bulk_delete_setup(client_as_alice)

    client_as_alice.post(
        "/api/admin/ontology/t1/relation-types/bulk-delete",
        json={"relation_types": ["SOLD_BY", "查无此关系"]},
        headers=_headers(),
    )

    deletions = [
        (c.actor, c.object_id)
        for c in _changes(conn_for_testing)
        if c.action == "delete" and c.object_kind == "relation_type"
    ]
    assert deletions == [("alice", "SOLD_BY")]


def test_bulk_delete_constraints_deletes_what_it_can_and_names_each_failure(client_as_alice):
    _bulk_delete_setup(client_as_alice)
    client_as_alice.post(
        "/api/admin/ontology/t1/constraints",
        json={
            "subject_term_type": "公司", "relation_type": "SOLD_BY", "object_term_type": "产品",
        },
        headers=_headers(),
    )

    resp = client_as_alice.post(
        "/api/admin/ontology/t1/constraints/bulk-delete",
        json={
            "constraints": [
                {"subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "公司"},
                {"subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "产品"},
            ]
        },
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["requested"], body["deleted"]) == (2, 1)
    # key 用跟变更日志同一种写法（"主语 -关系-> 宾语"）：约束没有单列主键，
    # 它的身份就是那个三元组。
    assert [(f["key"], "不存在" in f["reason"]) for f in body["failures"]] == [
        ("产品 -SOLD_BY-> 产品", True)
    ]
    remaining = [
        (c["subject_term_type"], c["relation_type"], c["object_term_type"])
        for c in client_as_alice.get(
            "/api/admin/ontology/t1/constraints", headers=_headers()
        ).json()["constraints"]
    ]
    assert ("产品", "SOLD_BY", "公司") not in remaining
    assert ("公司", "SOLD_BY", "产品") in remaining


def test_bulk_delete_constraints_logs_only_the_ones_it_actually_deleted(
    client_as_alice, conn_for_testing
):
    _bulk_delete_setup(client_as_alice)

    client_as_alice.post(
        "/api/admin/ontology/t1/constraints/bulk-delete",
        json={
            "constraints": [
                {"subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "公司"},
                {"subject_term_type": "产品", "relation_type": "SOLD_BY", "object_term_type": "产品"},
            ]
        },
        headers=_headers(),
    )

    deletions = [
        (c.actor, c.object_id)
        for c in _changes(conn_for_testing)
        if c.action == "delete" and c.object_kind == "constraint"
    ]
    assert deletions == [("alice", "产品 -SOLD_BY-> 公司")]


def test_bulk_delete_routes_reject_unknown_tenant(client):
    for path, payload in (
        ("term-types/bulk-delete", {"values": ["x"]}),
        ("relation-types/bulk-delete", {"relation_types": ["X"]}),
        (
            "constraints/bulk-delete",
            {"constraints": [{"subject_term_type": "a", "relation_type": "R", "object_term_type": "b"}]},
        ),
    ):
        resp = client.post(
            f"/api/admin/ontology/no_such_tenant/{path}", json=payload, headers=_headers()
        )
        assert resp.status_code == 404, (path, resp.text)


# ---------------------------------------------------------------------------
# 确认本体前校验存量日期值（Task 7）
#
# 图里的日期是字符串属性，范围过滤靠字典序。字段刚被改成 date 类型时，如果
# 图里还躺着 "2026/1/15" 这类没归一的值，这些实体在按时间过滤时会被静默
# 漏掉——不报错，只是查不出来。所以要在 update_term_type 写的草稿被
# confirm 提升为确定版本的那一刻挡住。
# ---------------------------------------------------------------------------


def _replace_draft_with_purchase_date_field(client, *, value_type: str, tenant_id: str = "t1"):
    return client.post(
        f"/api/admin/ontology/{tenant_id}/draft/replace",
        json={
            "term_types": [
                {
                    "value": "订单",
                    "extra_fields": [{"name": "purchase_date", "value_type": value_type}],
                }
            ],
            "relation_types": [],
            "constraints": [],
        },
        headers=_headers(),
    )


def test_confirming_a_new_date_field_with_dirty_values_is_refused(client):
    """图里还躺着 2026/1/15 这类值时不能确认。

    放行的话这些实体在按时间过滤时会被静默漏掉——字典序把它们排到了
    十月之后，而没有任何地方会报错。
    """
    graph = _FakeGraphClient()
    graph.non_iso_by_field["purchase_date"] = (12, ["2026/1/15", "待定"])
    app.dependency_overrides[deps.get_graph_client] = lambda: graph

    # 已确认版本里 purchase_date 是 string
    assert _replace_draft_with_purchase_date_field(client, value_type="string").status_code == 200
    assert client.post("/api/admin/ontology/t1/confirm", headers=_headers()).status_code == 200

    # 草稿把它改成 date
    assert _replace_draft_with_purchase_date_field(client, value_type="date").status_code == 200
    response = client.post("/api/admin/ontology/t1/confirm", headers=_headers())

    assert response.status_code == 409
    detail = response.json()["detail"]
    # 数量和样例都要有：只说"有不合格的值"回答不了"我该去修什么"。
    assert "12" in detail and "2026/1/15" in detail and "purchase_date" in detail


def test_confirming_a_new_date_field_with_clean_values_goes_through(client):
    graph = _FakeGraphClient()  # non_iso_by_field 空 → 一律 (0, [])
    app.dependency_overrides[deps.get_graph_client] = lambda: graph

    assert _replace_draft_with_purchase_date_field(client, value_type="string").status_code == 200
    assert client.post("/api/admin/ontology/t1/confirm", headers=_headers()).status_code == 200

    assert _replace_draft_with_purchase_date_field(client, value_type="date").status_code == 200
    response = client.post("/api/admin/ontology/t1/confirm", headers=_headers())
    assert response.status_code == 200


def test_a_field_that_was_already_a_date_is_not_rescanned(client):
    """没变的字段不查图——否则每次确认本体都要扫一遍全图。

    用 date_scan_calls 断言空，而不是断言"确认成功"：后者在"每次都扫、
    但恰好没有脏值"的实现下也是绿的。
    """
    graph = _FakeGraphClient()
    app.dependency_overrides[deps.get_graph_client] = lambda: graph

    # 已确认版本里 purchase_date 就已经是 date（新建类型本身也不扫，见下一条用例）
    assert _replace_draft_with_purchase_date_field(client, value_type="date").status_code == 200
    assert client.post("/api/admin/ontology/t1/confirm", headers=_headers()).status_code == 200
    assert graph.date_scan_calls == []

    # 草稿没改它，还是 date
    assert _replace_draft_with_purchase_date_field(client, value_type="date").status_code == 200
    response = client.post("/api/admin/ontology/t1/confirm", headers=_headers())

    assert response.status_code == 200
    assert graph.date_scan_calls == []


def test_a_brand_new_term_type_with_a_date_field_is_not_scanned(client):
    """新建的实体类型图里一个节点都没有，扫它是白扫。

    这条同时挡住"只要草稿里有 date 字段就扫"的实现。
    """
    graph = _FakeGraphClient()
    app.dependency_overrides[deps.get_graph_client] = lambda: graph

    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"}
            ],
            "relation_types": [],
            "constraints": [],
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    assert client.post("/api/admin/ontology/t1/confirm", headers=_headers()).status_code == 200

    # 草稿新增一个已确认版本里不存在的 term_type，带 date 字段
    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"},
                {
                    "value": "发货单",
                    "extra_fields": [{"name": "ship_date", "value_type": "date"}],
                },
            ],
            "relation_types": [],
            "constraints": [],
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    response = client.post("/api/admin/ontology/t1/confirm", headers=_headers())

    assert response.status_code == 200
    assert graph.date_scan_calls == []
