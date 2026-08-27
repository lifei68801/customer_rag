import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.terms_store import create_term, ensure_terms_schema, list_terms, update_term
from app.main import app
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_terms_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    # confirm_ontology/checkout_draft 需要 tenant_relation_types/
    # term_type_relation_allowlist 等表存在——ensure_terms_schema 只建
    # ontology_term_types 一张分类表，这里补齐完整的
    # 本体生命周期表结构（幂等，与 ensure_categories_schema 不冲突）。
    await ensure_ontology_schema(conn)
    # 既有测试直接用这些字面量当 term_type，早于分类枚举表存在——
    # 这里补齐分类，保持既有测试的字面量不变（见 test_terms_store.py 的
    # _connect() 同款说明）。term_type 现在按租户隔离，需要给每个测试里用到的
    # 租户各注册一份。真实术语只认
    # 已确认的实体类型（见 _validate_categories），这里创建完就立刻确认。
    for tenant_id in ("t1", "tenant_a"):
        await create_term_type(conn, tenant_id=tenant_id, value="error_code")
        await create_term_type(conn, tenant_id=tenant_id, value="t")
        await create_term_type(conn, tenant_id=tenant_id, value="t2")
        await confirm_ontology(conn, tenant_id)
    # Task 4：这个文件里的写接口（create_new_term/update_existing_term/
    # delete_existing_term）现在会先用 review_conn 调 require_active_tenant()
    # 校验 tenant_id——真实的 deps.get_review_conn() 会自动建好 tenants 表
    # 并回填历史租户，这里是手工建表的测试连接，绕开了那条路径，必须显式
    # 建表 + 注册本文件用例里出现过的 tenant_id（"t1"/"tenant_a"/"tenant_b"，
    # 和上面 term_type 分类注册用的租户集合保持一致）。
    await create_tenants_table(conn)
    for tenant_id in ("t1", "tenant_a", "tenant_b"):
        await create_tenant(conn, tenant_id=tenant_id, name=tenant_id)
    return conn


@pytest.fixture
def terms_conn():
    """术语表库连接（复用 graph_review_db_path 的连接，测试里用独立的
    :memory: 连接，只建 terms 表——路由层依赖的是 deps.get_review_conn，
    这里的 fixture 名字叫 terms_conn 只是强调这次测试关注的是术语表这
    部分，物理上和 review_conn 是同一类连接）。必须显式 close：见
    test_admin_graph_review_routes.py 里 review_conn fixture 的同款说明。
    """
    conn = asyncio.run(_open_terms_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


class SpyGraphClient:
    def __init__(self, *, edge_count: int = 0) -> None:
        self.synced: list[dict] = []
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._edge_count = edge_count
        # 记录 rename/sync 两类调用的相对顺序——renamed/synced 是两个独立列表，
        # 光看它们各自的内容看不出谁先谁后；改名场景要求 rename_term_node 必须
        # 在 sync_term 之前调用（sync_term 是按"当前"standard_name MERGE 匹配
        # 节点的，调用顺序反了会在旧名字下留一个没同步到的节点、新名字下多建
        # 一个空节点），这个列表就是用来证明调用顺序没被后续改动意外交换的。
        self.call_order: list[str] = []

    async def sync_term(self, term) -> None:
        self.call_order.append("sync_term")
        self.synced.append(
            {
                "standard_name": term.standard_name,
                "aliases": term.aliases,
                "term_type": term.term_type,
            }
        )

    async def rename_term_node(self, *, tenant_id: str, node_key: str, new_standard_name: str) -> None:
        self.call_order.append("rename_term_node")
        self.renamed.append((node_key, new_standard_name))

    async def count_relation_edges_for_term(self, *, tenant_id: str, node_key: str) -> int:
        return self._edge_count

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        self.deleted.append(node_key)


def test_list_terms_returns_all_terms(terms_conn):
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="错误码E502", aliases=["网关超时"],
            term_type="error_code",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/t1/terms", headers=_authed_headers(session_store))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["terms"] == [
        {
            "standard_name": "错误码E502", "aliases": ["网关超时"],
            "term_type": "error_code",
            "extra_properties": {}, "source": "manual",
            # Task 3：TermResponse 新增 similar_terms 字段——list_all_terms
            # 走的是 _to_response(term)，不传 similar_terms 参数，默认值 None
            # 会原样出现在 JSON 响应里，这里更新期望值以匹配新 schema。
            "similar_terms": None,
        }
    ]


def test_list_terms_paginates_with_page_and_page_size(terms_conn):
    """种 3 条术语（按 standard_name 排序为 A、B、C），GET ?page=2&page_size=1
    应该只返回第 2 条（B），并且 total 字段反映该租户的全部术语数（3），
    不受当前这一页大小的影响。"""
    for name in ("A", "B", "C"):
        asyncio.run(
            create_term(
                terms_conn, tenant_id="t1", standard_name=name, aliases=[],
                term_type="t",
            )
        )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/terms",
            params={"page": 2, "page_size": 1},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert [t["standard_name"] for t in body["terms"]] == ["B"]
    assert body["total"] == 3


def test_list_terms_without_page_params_returns_full_list_beyond_default_page_size(terms_conn):
    """回归测试：list_all_terms 的 page/page_size 曾经默认为 1/20，导致
    termsApi.ts 里不传任何 query 参数的 fetchTerms()（GraphReviewsPage.tsx
    标准名自动补全用它拉全量数据）在术语数超过 20 条时被后端悄悄截断成
    只有第一页。这里种 21 条术语，不传 page/page_size 请求，断言拿到的是
    全部 21 条而不是被截断的 20 条，且和 total 字段一致。"""
    for i in range(21):
        asyncio.run(
            create_term(
                terms_conn, tenant_id="t1", standard_name=f"术语{i:02d}", aliases=[],
                term_type="t",
            )
        )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/t1/terms", headers=_authed_headers(session_store))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["terms"]) == 21
    assert len(body["terms"]) > 20
    assert body["total"] == 21
    assert len(body["terms"]) == body["total"]


def test_list_terms_without_session_token_returns_401(terms_conn):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/t1/terms")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_create_term_syncs_to_graph_client(terms_conn):
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "新术语", "aliases": ["别名1"],
                "term_type": "t",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(graph_client.synced) == 1
    assert graph_client.synced[0]["standard_name"] == "新术语"


def test_create_term_returns_404_for_unknown_tenant(terms_conn):
    """Task 4：写接口在具体业务逻辑之前要先校验 tenant_id 在 tenants
    注册表里存在且是 active——一个从未注册过的 tenant_id 应该直接 404，
    而不是被当作合法租户走完创建流程。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/no-such-tenant/terms",
            json={"standard_name": "新术语", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_create_term_with_conflicting_name_returns_400(terms_conn):
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="已存在", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={"standard_name": "已存在", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_create_term_with_unknown_category_returns_400(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "新术语", "aliases": [],
                "term_type": "没有这个分类",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_update_term_without_rename_syncs_to_graph_client(terms_conn):
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="术语A", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/术语A?term_type=t",
            json={
                "standard_name": "术语A", "aliases": ["新别名"],
                "term_type": "t2",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.renamed == []
    assert len(graph_client.synced) == 1
    assert graph_client.synced[0]["aliases"] == ["新别名"]


def test_update_term_with_rename_calls_rename_then_sync(terms_conn):
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="旧名字", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/旧名字?term_type=t",
            json={"standard_name": "新名字", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.renamed == [("t:旧名字", "新名字")]
    assert graph_client.synced[0]["standard_name"] == "新名字"
    assert graph_client.call_order == ["rename_term_node", "sync_term"]


def test_update_term_rename_into_existing_name_returns_400(terms_conn):
    asyncio.run(create_term(terms_conn, tenant_id="t1", standard_name="A", aliases=[], term_type="t"))
    asyncio.run(create_term(terms_conn, tenant_id="t1", standard_name="B", aliases=[], term_type="t"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/A?term_type=t",
            json={"standard_name": "B", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_update_nonexistent_term_returns_404(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/不存在?term_type=t",
            json={"standard_name": "不存在", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_delete_term_without_graph_edges_succeeds(terms_conn):
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="待删除", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient(edge_count=0)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.delete(
            "/api/admin/t1/terms/待删除?term_type=t", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.deleted == ["t:待删除"]


def test_delete_nonexistent_term_returns_404_even_when_graph_has_edges(terms_conn):
    """404 优先于 409：一个 SQLite 里根本不存在的名字，即使图谱里凑巧有
    同名的边（比如迁移前遗留的孤儿数据），也应该报"不存在"而不是"已在
    图谱中使用"——后者会误导管理员去查一个其实从未在词表里存在过的
    术语。"""
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient(edge_count=5)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.delete(
            "/api/admin/t1/terms/不存在?term_type=t", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_delete_term_with_graph_edges_returns_409(terms_conn):
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="使用中", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient(edge_count=2)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.delete(
            "/api/admin/t1/terms/使用中?term_type=t", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert graph_client.deleted == []
    # SQLite 记录也不该被删掉——409 之后术语表和图谱两边都保持原样
    remaining = asyncio.run(list_terms(terms_conn, "t1"))
    assert [t.standard_name for t in remaining] == ["使用中"]


def test_create_term_with_empty_standard_name_returns_422(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={"standard_name": "   ", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_create_term_with_slash_in_standard_name_returns_422(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={"standard_name": "A/B测试", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_update_term_rename_into_name_with_slash_returns_422(terms_conn):
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="旧名字", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/旧名字",
            json={"standard_name": "A/B", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_create_term_drops_blank_aliases(terms_conn):
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "新术语",
                "aliases": ["别名1", "  ", "", "别名2"],
                "term_type": "t",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["aliases"] == ["别名1", "别名2"]


def test_update_term_drops_blank_aliases(terms_conn):
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="术语A", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/术语A?term_type=t",
            json={
                "standard_name": "术语A",
                "aliases": ["  别名1  ", "", "   "],
                "term_type": "t",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["aliases"] == ["别名1"]


def test_create_term_with_typed_extra_properties_returns_200(terms_conn):
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="VariantValue",
            extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "容量750ml", "aliases": [], "term_type": "VariantValue",
                "extra_properties": {"numeric_value": 750},
            },
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 200
        assert response.json()["extra_properties"] == {"numeric_value": 750}
    finally:
        app.dependency_overrides.clear()


def test_create_term_rejects_extra_property_wrong_type_returns_400(terms_conn):
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="VariantValue",
            extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "X", "aliases": [], "term_type": "VariantValue",
                "extra_properties": {"numeric_value": "不是数字"},
            },
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_create_term_is_scoped_to_tenant_in_url(terms_conn):
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/tenant_a/terms",
            json={"standard_name": "新术语", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 200
        assert response.json()["standard_name"] == "新术语"

        list_resp = client.get("/api/admin/tenant_a/terms", headers=_authed_headers(session_store))
        assert len(list_resp.json()["terms"]) == 1

        other_tenant_resp = client.get("/api/admin/tenant_b/terms", headers=_authed_headers(session_store))
        assert other_tenant_resp.json()["terms"] == []
    finally:
        app.dependency_overrides.clear()


def test_create_term_rejects_bool_extra_property_via_http(terms_conn):
    """Pydantic 不能再把 JSON true/false 静默转成 1/0——必须让
    terms_store.py 的类型校验器看到真正的 bool 值并拒绝它。"""
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="VariantValue",
            extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "X", "aliases": [], "term_type": "VariantValue",
                "extra_properties": {"numeric_value": True},
            },
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_create_term_returns_source(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "term-x", "aliases": [], "term_type": "t",
                "source": "review",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["source"] == "review"


def test_create_term_without_source_defaults_to_manual(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={"standard_name": "term-y", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["source"] == "manual"


def test_create_term_with_invalid_source_returns_422(terms_conn):
    """Fix 6 回归测试：TermWriteRequest.source 现在是 Literal["manual",
    "etl", "review", "unknown"]，不再接受任意字符串——一个不在枚举里的
    值应该被 FastAPI/Pydantic 挡在 422，而不是静默持久化进 terms 表，
    落到"来源"筛选下拉框的枚举选项之外。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "term-z", "aliases": [], "term_type": "t",
                "source": "not-a-real-source",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_list_terms_filters_by_source_query_param(terms_conn):
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="手工术语", aliases=[],
            term_type="t", source="manual",
        )
    )
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="ETL术语", aliases=[],
            term_type="t", source="etl",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/terms",
            params={"source": "etl"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert [t["standard_name"] for t in body["terms"]] == ["ETL术语"]
    assert body["total"] == 1


def test_update_term_preserves_source_regardless_of_payload(terms_conn):
    """update_term 从不修改 source（terms_store.py Task 1 的既有行为）——
    即使请求体里带了别的 source 值，响应里的 source 也应该保持术语创建时
    的原始值，因为 update_existing_term 是从 existing_before_update.source
    构造响应用的 Term 对象，而不是从 payload.source。"""
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="术语B", aliases=[],
            term_type="t", source="etl",
        )
    )
    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/术语B?term_type=t",
            json={
                "standard_name": "术语B", "aliases": [], "term_type": "t",
                "source": "manual",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["source"] == "etl"


def test_update_term_requires_term_type_query_param(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        create_response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "Coffee", "aliases": [], "term_type": "t",
                "extra_properties": {}, "source": "manual",
            },
            headers=_authed_headers(session_store),
        )
        assert create_response.status_code == 200

        response = client.put(
            "/api/admin/t1/terms/Coffee",
            json={
                "standard_name": "拿铁", "aliases": [], "term_type": "t",
                "extra_properties": {}, "source": "manual",
            },
            headers=_authed_headers(session_store),
        )

        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_delete_term_requires_term_type_query_param(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        create_response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "Coffee", "aliases": [], "term_type": "t",
                "extra_properties": {}, "source": "manual",
            },
            headers=_authed_headers(session_store),
        )
        assert create_response.status_code == 200

        response = client.delete(
            "/api/admin/t1/terms/Coffee", headers=_authed_headers(session_store)
        )

        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_update_and_delete_term_disambiguate_by_term_type(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "Coffee", "aliases": [], "term_type": "t",
                "extra_properties": {}, "source": "manual",
            },
            headers=_authed_headers(session_store),
        )
        client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "Coffee", "aliases": [], "term_type": "t2",
                "extra_properties": {}, "source": "manual",
            },
            headers=_authed_headers(session_store),
        )

        update_response = client.put(
            "/api/admin/t1/terms/Coffee?term_type=t",
            json={
                "standard_name": "拿铁", "aliases": [], "term_type": "t",
                "extra_properties": {}, "source": "manual",
            },
            headers=_authed_headers(session_store),
        )
        assert update_response.status_code == 200
        assert update_response.json()["standard_name"] == "拿铁"

        delete_response = client.delete(
            "/api/admin/t1/terms/Coffee?term_type=t2", headers=_authed_headers(session_store)
        )
        assert delete_response.status_code == 200

        list_response = client.get("/api/admin/t1/terms", headers=_authed_headers(session_store))
        remaining_names = {t["standard_name"] for t in list_response.json()["terms"]}
        assert remaining_names == {"拿铁"}
    finally:
        app.dependency_overrides.clear()


def test_create_term_returns_similar_terms_hint(terms_conn):
    """Task 3：创建新术语时，响应体里附带一份跟现有同类型术语的相似度
    提示——先建一条 standard_name="Coca-Cola"、别名带"可乐"的术语，再
    创建 standard_name="可口可乐" 的新术语。新旧标准名/别名互不完全
    相同（否则会先撞上 _check_name_conflict 的精确名字冲突检测，测不到
    这里要验证的相似度提示），但"可乐"是"可口可乐"的连续子串，
    longest_common_substring_score 按重叠长度/候选别名长度算出 2/2 = 1.0。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "Coca-Cola", "aliases": ["可乐"],
                "term_type": "t", "extra_properties": {},
            },
            headers=_authed_headers(session_store),
        )

        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "可口可乐", "aliases": [],
                "term_type": "t", "extra_properties": {},
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["similar_terms"]) == 1
    assert body["similar_terms"][0]["standard_name"] == "Coca-Cola"
    assert body["similar_terms"][0]["similarity_score"] == 1.0


def test_create_term_no_similar_terms_returns_empty_list(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "完全独特的名字XYZ123", "aliases": [],
                "term_type": "t", "extra_properties": {},
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["similar_terms"] == []


def test_create_term_excludes_tombstoned_terms_from_similar_terms_hint(terms_conn):
    """Fix 1：创建时的相似度提示不该把已经被合并（duplicate_review_queue.
    approve_duplicate_suggestion 打上"[已合并] "墓碑标记）的行当成"这个
    新名字看起来很像"的候选。墓碑串是"[已合并] {node_key}"，node_key 带着
    被合并前的原始 standard_name（这里是"可口可乐股份"，node_key
    "t:可口可乐股份"）；新建术语的标准名"可口可乐"是这个墓碑串里的一个
    连续子串，longest_common_substring_score 按 重叠长度/min(len(a),len(b))
    算出 1.0——不过滤的话这条墓碑行会作为"相似"提示出现，见 Fix 1 的
    调查记录。"""
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="可口可乐股份", aliases=[],
            term_type="t",
        )
    )
    asyncio.run(
        update_term(
            terms_conn, tenant_id="t1", standard_name="可口可乐股份",
            new_standard_name="[已合并] t:可口可乐股份", aliases=[],
            term_type="t", current_term_type="t",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "可口可乐", "aliases": [],
                "term_type": "t", "extra_properties": {},
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["similar_terms"] == []


def test_create_term_rejects_bool_inside_number_array_via_http(terms_conn):
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="VariantValue",
            extra_fields=[ExtraFieldSpec(name="dims", value_type="number[]")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "Y", "aliases": [], "term_type": "VariantValue",
                "extra_properties": {"dims": [1.0, True]},
            },
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()
