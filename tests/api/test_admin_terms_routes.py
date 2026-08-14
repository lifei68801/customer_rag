import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.graphrag.ontology_categories import create_product_line, create_term_type
from app.graphrag.terms_store import create_term, ensure_terms_schema, list_terms
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
        admin_token="tok",
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _open_terms_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    # 既有测试直接用这些字面量当 term_type/product_line，早于分类枚举表存在——
    # 这里补齐分类，保持既有测试的字面量不变（见 test_terms_store.py 的
    # _connect() 同款说明）。
    await create_term_type(conn, value="error_code")
    await create_term_type(conn, value="t")
    await create_term_type(conn, value="t2")
    await create_product_line(conn, value="核心平台")
    await create_product_line(conn, value="p")
    await create_product_line(conn, value="p2")
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
                "product_line": term.product_line,
            }
        )

    async def rename_term_node(self, *, old_name: str, new_name: str) -> None:
        self.call_order.append("rename_term_node")
        self.renamed.append((old_name, new_name))

    async def count_relation_edges_for_term(self, standard_name: str) -> int:
        return self._edge_count

    async def delete_term_node(self, standard_name: str) -> None:
        self.deleted.append(standard_name)


def test_list_terms_returns_all_terms(terms_conn):
    asyncio.run(
        create_term(
            terms_conn, standard_name="错误码E502", aliases=["网关超时"],
            term_type="error_code", product_line="核心平台",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/terms", headers=_authed_headers(session_store))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["terms"] == [
        {
            "standard_name": "错误码E502", "aliases": ["网关超时"],
            "term_type": "error_code", "product_line": "核心平台",
            "extra_properties": {},
        }
    ]


def test_list_terms_without_session_token_returns_401(terms_conn):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/terms")
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
            "/api/admin/terms",
            json={
                "standard_name": "新术语", "aliases": ["别名1"],
                "term_type": "t", "product_line": "p",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(graph_client.synced) == 1
    assert graph_client.synced[0]["standard_name"] == "新术语"


def test_create_term_with_conflicting_name_returns_400(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="已存在", aliases=[], term_type="t", product_line="p")
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/terms",
            json={"standard_name": "已存在", "aliases": [], "term_type": "t", "product_line": "p"},
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
            "/api/admin/terms",
            json={
                "standard_name": "新术语", "aliases": [],
                "term_type": "没有这个分类", "product_line": "p",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_update_term_without_rename_syncs_to_graph_client(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="术语A", aliases=[], term_type="t", product_line="p")
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
            "/api/admin/terms/术语A",
            json={
                "standard_name": "术语A", "aliases": ["新别名"],
                "term_type": "t2", "product_line": "p2",
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
        create_term(terms_conn, standard_name="旧名字", aliases=[], term_type="t", product_line="p")
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
            "/api/admin/terms/旧名字",
            json={"standard_name": "新名字", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.renamed == [("旧名字", "新名字")]
    assert graph_client.synced[0]["standard_name"] == "新名字"
    assert graph_client.call_order == ["rename_term_node", "sync_term"]


def test_update_term_rename_into_existing_name_returns_400(terms_conn):
    asyncio.run(create_term(terms_conn, standard_name="A", aliases=[], term_type="t", product_line="p"))
    asyncio.run(create_term(terms_conn, standard_name="B", aliases=[], term_type="t", product_line="p"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/terms/A",
            json={"standard_name": "B", "aliases": [], "term_type": "t", "product_line": "p"},
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
            "/api/admin/terms/不存在",
            json={"standard_name": "不存在", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_delete_term_without_graph_edges_succeeds(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="待删除", aliases=[], term_type="t", product_line="p")
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
            "/api/admin/terms/待删除", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.deleted == ["待删除"]


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
            "/api/admin/terms/不存在", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_delete_term_with_graph_edges_returns_409(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="使用中", aliases=[], term_type="t", product_line="p")
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
            "/api/admin/terms/使用中", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert graph_client.deleted == []
    # SQLite 记录也不该被删掉——409 之后术语表和图谱两边都保持原样
    remaining = asyncio.run(list_terms(terms_conn))
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
            "/api/admin/terms",
            json={"standard_name": "   ", "aliases": [], "term_type": "t", "product_line": "p"},
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
            "/api/admin/terms",
            json={"standard_name": "A/B测试", "aliases": [], "term_type": "t", "product_line": "p"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_update_term_rename_into_name_with_slash_returns_422(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="旧名字", aliases=[], term_type="t", product_line="p")
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/terms/旧名字",
            json={"standard_name": "A/B", "aliases": [], "term_type": "t", "product_line": "p"},
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
            "/api/admin/terms",
            json={
                "standard_name": "新术语",
                "aliases": ["别名1", "  ", "", "别名2"],
                "term_type": "t",
                "product_line": "p",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["aliases"] == ["别名1", "别名2"]


def test_update_term_drops_blank_aliases(terms_conn):
    asyncio.run(
        create_term(terms_conn, standard_name="术语A", aliases=[], term_type="t", product_line="p")
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
            "/api/admin/terms/术语A",
            json={
                "standard_name": "术语A",
                "aliases": ["  别名1  ", "", "   "],
                "term_type": "t",
                "product_line": "p",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["aliases"] == ["别名1"]
