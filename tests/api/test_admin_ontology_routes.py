from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
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

    async def sync_term(self, term) -> None:
        pass

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
                "extra_fields": [{"name": "severity_level", "value_type": "string"}],
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
                    {"name": "numeric_value", "value_type": "number"},
                    {"name": "dims", "value_type": "number[]"},
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
