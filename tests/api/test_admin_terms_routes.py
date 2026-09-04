import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import (
    EXTRA_PROPERTY_PREFIX,
    FIELD_CREATED,
    FIELD_DELETED,
    FIELD_EXTRA_PROPERTIES,
    ensure_term_edits_schema,
    list_term_edits_for_node_key,
    upsert_term_edit,
)
from app.graphrag.terms_store import create_term, ensure_terms_schema, list_terms, update_term
from app.main import app
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_terms_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    # Task 4：这个文件的写接口现在把编辑写进 term_edits，不再是 terms——
    # 手工建表的测试连接需要显式建这张表，真实的
    # ontology_store.open_ontology_store_conn 会自动建（唯一的建表入口）。
    await ensure_term_edits_schema(conn)
    # confirm_ontology/checkout_draft 需要 tenant_relation_types/
    # term_type_relation_allowlist 等表存在——ensure_terms_schema 只建
    # ontology_term_types 一张分类表，这里补齐完整的
    # 本体生命周期表结构（幂等，与 ensure_categories_schema 不冲突）。
    await ensure_ontology_schema(conn)
    # 既有测试直接用这些字面量当 term_type，早于分类枚举表存在——
    # 这里补齐分类，保持既有测试的字面量不变（见 test_terms_store.py 的
    # _connect() 同款说明）。term_type 现在按租户隔离，需要给每个测试里用到的
    # 租户各注册一份。真实术语只认
    # 已确认的实体类型（见 validate_term_categories），这里创建完就立刻确认。
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
    # require_admin_session 现在每个请求都要确认账号仍是 active，
    # 所以本体库里必须有这张表和一个可用的管理员。
    await ensure_admin_users_schema(conn)
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
    # 租户标记异常的边那条路径要区分 admin 和 member 的权限，两个身份都得
    # 有一个真实存在且 active 的账号——require_admin_session 每个请求都查库。
    await create_admin_user(
        conn, username="member1", password="password1", role="member", tenant_id="t1"
    )
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
    token = session_store.create_session(username="admin", role="admin", tenant_id=None)
    return {"Authorization": f"Bearer {token}"}


def _member_headers(session_store: AdminSessionStore) -> dict[str, str]:
    """t1 租户的普通成员。跨租户的脏边只有平台管理员能删，这个身份用来钉住
    "member 借这条路径删不到无权的边"。"""
    token = session_store.create_session(username="member1", role="member", tenant_id="t1")
    return {"Authorization": f"Bearer {token}"}


class SpyGraphClient:
    def __init__(
        self,
        *,
        edge_count: int = 0,
        relations: list[dict] | None = None,
        relations_error: Exception | None = None,
        removed_edges: int = 1,
        inconsistent: list[dict] | None = None,
    ) -> None:
        # 租户标记异常的边：列出来的内容，以及收到的删除定位参数。
        self._inconsistent = inconsistent or []
        self.listed_inconsistent: list[dict] = []
        self.deleted_inconsistent_edges: list[dict] = []
        # 删边接口：记录收到的定位参数，并模拟"实际删掉几条"。
        self.deleted_edges: list[dict] = []
        self._removed_edges = removed_edges
        # 删除被挡住时，409 里要点名挡路的是哪几条边——路由拿这两个字段
        # 模拟图客户端返回的关系明细/读取失败。
        self._relations = relations or []
        self._relations_error = relations_error
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
                "extra_properties": term.extra_properties,
            }
        )

    async def rename_term_node(self, *, tenant_id: str, node_key: str, new_standard_name: str) -> None:
        self.call_order.append("rename_term_node")
        self.renamed.append((node_key, new_standard_name))

    async def count_relation_edges_for_term(self, *, tenant_id: str, node_key: str) -> int:
        return self._edge_count

    async def delete_relation_edge(
        self, *, tenant_id: str, subject_node_key: str, relation_type: str,
        object_node_key: str,
    ) -> int:
        self.deleted_edges.append(
            {
                "tenant_id": tenant_id,
                "subject_node_key": subject_node_key,
                "relation_type": relation_type,
                "object_node_key": object_node_key,
            }
        )
        return self._removed_edges

    async def list_inconsistent_relation_edges(
        self, *, tenant_id: str, node_key: str
    ) -> list[dict]:
        self.listed_inconsistent.append({"tenant_id": tenant_id, "node_key": node_key})
        return self._inconsistent

    async def delete_inconsistent_relation_edge(
        self, *, subject_tenant_id: str, subject_node_key: str, relation_type: str,
        object_tenant_id: str, object_node_key: str,
    ) -> int:
        self.deleted_inconsistent_edges.append(
            {
                "subject_tenant_id": subject_tenant_id,
                "subject_node_key": subject_node_key,
                "relation_type": relation_type,
                "object_tenant_id": object_tenant_id,
                "object_node_key": object_node_key,
            }
        )
        return self._removed_edges

    async def list_term_relations(self, *, tenant_id: str, node_key: str) -> list[dict]:
        if self._relations_error is not None:
            raise self._relations_error
        return self._relations

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
            # Task 3：TermResponse 新增 node_key 字段——addressing 改成走
            # node_key 之后响应体里必须带上它，这里更新期望值以匹配新 schema。
            "node_key": "error_code:错误码E502",
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


def test_create_term_with_conflicting_name_merges_into_existing_row(terms_conn):
    """Task 4 前：这个用例断言同名冲突返回 400（_check_name_conflict）。

    Task 4 起该端点不再做名字冲突检查——这是刻意的，standard_name 早已
    不是身份键（2026-08-30 起同一 term_type 下允许重名），编辑层路径上
    "名字撞了"不再是数据完整性问题。这里提交的 standard_name/term_type
    算出的 node_key 恰好和已有的 terms 行相同，于是这次 POST 写的
    __created__ 编辑被合并视图当成对那一行的普通字段级编辑（见
    term_merge.apply_edits），不报错、也不会产生第二条记录。
    """
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

    assert response.status_code == 200
    # terms 表仍然只有原来那一条，没有因为这次 POST 多出一行。
    raw_terms = asyncio.run(list_terms(terms_conn, "t1"))
    assert [t.standard_name for t in raw_terms] == ["已存在"]


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
            "/api/admin/t1/terms/t:术语A",
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
            "/api/admin/t1/terms/t:旧名字",
            json={"standard_name": "新名字", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.renamed == [("t:旧名字", "新名字")]
    assert graph_client.synced[0]["standard_name"] == "新名字"
    assert graph_client.call_order == ["rename_term_node", "sync_term"]


def test_update_term_rename_into_existing_name_succeeds_without_conflict_check(terms_conn):
    """Task 4 前：这个用例断言改名撞了别的术语的名字返回 400
    （_check_name_conflict）。

    Task 4 起 update_term 不再是这个端点的写入路径，_check_name_conflict
    随之失去生产调用方——编辑层路径上不重建这道检查（同名多条术语在
    2026-08-30 之后本就是允许的合法状态，见 test_update_term_addresses_by_node_key
    这类用例）。node_key 是身份键，改名不改变它，所以 A 和 B 仍然是
    两条独立记录，只是现在展示名恰好相同。
    """
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
            "/api/admin/t1/terms/t:A",
            json={"standard_name": "B", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["standard_name"] == "B"
    assert response.json()["node_key"] == "t:A"
    # terms 表两条原始记录都还在，PUT 没有碰 terms 表。
    raw_terms = asyncio.run(list_terms(terms_conn, "t1"))
    assert sorted(t.standard_name for t in raw_terms) == ["A", "B"]


def test_update_nonexistent_term_returns_404(terms_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/t:不存在",
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
            "/api/admin/t1/terms/t:待删除", headers=_authed_headers(session_store)
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
            "/api/admin/t1/terms/t:不存在", headers=_authed_headers(session_store)
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
            "/api/admin/t1/terms/t:使用中", headers=_authed_headers(session_store)
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
            "/api/admin/t1/terms/t:术语A",
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
            "/api/admin/t1/terms/t:术语B",
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


def test_update_term_without_extra_properties_preserves_existing_values(terms_conn):
    """请求体里没有 extra_properties 时，术语已有的属性值必须原样保留。

    PUT 的全量替换语义对 standard_name/aliases 是对的，但对属性值太危险：
    管理界面的术语编辑表单只提交名字和别名，一次改名就会把这条术语的全部
    属性值静默抹掉——ETL 建模把度量列（金额、数量、日期）放进属性字段之后，
    这等于一次编辑丢掉一整行业务数据。语义因此对齐同一个文件里
    test_update_term_preserves_source_regardless_of_payload 已经确立的
    "更新时保留"约定：字段缺席 = 保留，显式传 {} 才是清空。
    """
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="订单号",
            extra_fields=[ExtraFieldSpec(name="revenue", value_type="number")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="1-143-51064-X", aliases=[],
            term_type="订单号", extra_properties={"revenue": 2141.0},
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/订单号:1-143-51064-X",
            json={"standard_name": "1-143-51064-X", "aliases": ["首单"], "term_type": "订单号"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["extra_properties"] == {"revenue": 2141.0}


def test_update_term_without_extra_properties_preserves_them_in_graph_too(terms_conn):
    """保留必须同时发生在图谱镜像上，不能只保住 SQLite。

    结构化查询走的是 Neo4j 上的属性，不是 SQLite——如果只有响应体和 SQLite
    保住了值而 sync_term 收到 {}，属性值会从图谱上被抹掉，两个存储之间出现
    只有属性值不一致的静默偏差，而查询结果会先坏掉。
    """
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="订单号",
            extra_fields=[ExtraFieldSpec(name="revenue", value_type="number")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="1-143-51064-X", aliases=[],
            term_type="订单号", extra_properties={"revenue": 2141.0},
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
            "/api/admin/t1/terms/订单号:1-143-51064-X",
            json={"standard_name": "1-143-51064-X", "aliases": ["首单"], "term_type": "订单号"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(graph_client.synced) == 1
    assert graph_client.synced[0]["extra_properties"] == {"revenue": 2141.0}


def test_update_term_with_empty_extra_properties_clears_them(terms_conn):
    """显式传 {} 是"清空属性值"，不能和字段缺席混为一谈。

    没有这条区分，"保留"就退化成"永远无法清空"——属性值一旦写进去就再也
    删不掉了。这是上一个测试的另一半：缺席=保留，{}=清空。

    这条断言曾在 Task 4 的第一版实现里被误改成"{} 也保留"（field-level
    单键编辑确实没有"删掉某个键"的语义），但契约本身没变
    （TermWriteRequest 的文档字符串、frontend/src/admin/termsApi.ts 都
    明文写了"缺席=保留、{}=清空"）——修法是给编辑层加一种新的编辑粒度
    （FIELD_EXTRA_PROPERTIES 整字典编辑），不是悄悄改契约。见
    term_merge._apply_field_edits 里 FIELD_EXTRA_PROPERTIES 作为基底、
    EXTRA_PROPERTY_PREFIX 单键编辑叠加其上的合并顺序。
    """
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="订单号",
            extra_fields=[ExtraFieldSpec(name="revenue", value_type="number")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="1-143-51064-X", aliases=[],
            term_type="订单号", extra_properties={"revenue": 2141.0},
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/订单号:1-143-51064-X",
            json={
                "standard_name": "1-143-51064-X", "aliases": [], "term_type": "订单号",
                "extra_properties": {},
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["extra_properties"] == {}
    # terms 表那一行的属性值没变——PUT 只写 term_edits，清空是通过
    # FIELD_EXTRA_PROPERTIES 整字典编辑覆盖出来的，不是真的删了什么。
    raw_terms = asyncio.run(list_terms(terms_conn, "t1"))
    assert raw_terms[0].extra_properties == {"revenue": 2141.0}
    edits = asyncio.run(
        list_term_edits_for_node_key(terms_conn, "t1", "订单号:1-143-51064-X")
    )
    assert edits[FIELD_EXTRA_PROPERTIES] == {}
    # FIELD_EXTRA_PROPERTIES（"extra_properties"）本身不以
    # EXTRA_PROPERTY_PREFIX（"extra_properties."）开头，这里顺带确认
    # 没有另外多写出单键编辑。
    assert not any(field.startswith(EXTRA_PROPERTY_PREFIX) for field in edits)


# Task 3：test_update_term_requires_term_type_query_param 和
# test_delete_term_requires_term_type_query_param（曾经断言 PUT/DELETE
# 缺少 term_type query 参数时返回 422）在这个任务里被删除，不是转换成
# node_key 形式——它们测的行为本身（"term_type 是必填 query 参数"）随着
# 寻址方式改成 node_key 一起被移除了：PUT/DELETE 的路由签名里已经没有
# term_type 这个参数，FastAPI 对未声明的多余 query 参数从不报错，所以
# "缺 term_type 时 422" 这个断言不再对应任何真实校验路径——旧路径下的
# "/Coffee"（不带 node_key 前缀）现在会被当成字面 node_key 去查、查不到
# 返回 404，而不是 422。没有等价的新行为可以顶替这两条用例，同名多条时
# 必须用 node_key 精确寻址这件事已经由
# test_update_term_addresses_by_node_key（tests/api/test_admin_terms_routes.py）
# 和下面重写后的 test_update_and_delete_term_disambiguate_by_node_key 覆盖。


def test_update_and_delete_term_disambiguate_by_node_key(terms_conn):
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
            "/api/admin/t1/terms/t:Coffee",
            json={
                "standard_name": "拿铁", "aliases": [], "term_type": "t",
                "extra_properties": {}, "source": "manual",
            },
            headers=_authed_headers(session_store),
        )
        assert update_response.status_code == 200
        assert update_response.json()["standard_name"] == "拿铁"

        delete_response = client.delete(
            "/api/admin/t1/terms/t2:Coffee", headers=_authed_headers(session_store)
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
            terms_conn, tenant_id="t1", node_key="t:可口可乐股份",
            new_standard_name="[已合并] t:可口可乐股份", aliases=[],
            term_type="t",
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


def test_update_term_addresses_by_node_key(terms_conn):
    """PUT 用 node_key 寻址——同名多条时按名字寻址无法确定改哪一条。"""
    from app.graphrag.terms_store import upsert_term_with_node_key
    asyncio.run(
        upsert_term_with_node_key(
            terms_conn, tenant_id="t1", node_key="t:张三:200", standard_name="张三",
            aliases=[], term_type="t", extra_properties={},
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/t:张三:200",
            json={"standard_name": "张三改", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["standard_name"] == "张三改"
    assert response.json()["node_key"] == "t:张三:200"


# ---------------------------------------------------------------------------
# Task 4：POST/PUT/DELETE 写编辑层，不再写 terms 表。
# ---------------------------------------------------------------------------


def test_put_writes_an_edit_and_never_touches_the_terms_table(terms_conn):
    """Global Constraints 第一条：人工编辑路径永不写 terms。违反了的话
    "重跑 ETL 不伤人工修正"的保证就静默失效了。"""
    from app.graphrag.terms_store import upsert_term_with_node_key
    asyncio.run(
        upsert_term_with_node_key(
            terms_conn, tenant_id="t1", node_key="t:ETL实体", standard_name="ETL实体",
            aliases=[], term_type="t", extra_properties={},
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/t:ETL实体",
            json={"standard_name": "人工改名", "aliases": [], "term_type": "t"},
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 200

        # terms 表那一行的 standard_name 没变——PUT 只写 term_edits。
        raw_terms = asyncio.run(list_terms(terms_conn, "t1"))
        assert [t.standard_name for t in raw_terms] == ["ETL实体"]

        edits = asyncio.run(list_term_edits_for_node_key(terms_conn, "t1", "t:ETL实体"))
        assert edits["standard_name"] == "人工改名"

        # 读端点走合并视图，返回人工值。
        list_response = client.get("/api/admin/t1/terms", headers=_authed_headers(session_store))
    finally:
        app.dependency_overrides.clear()

    names = {t["node_key"]: t["standard_name"] for t in list_response.json()["terms"]}
    assert names["t:ETL实体"] == "人工改名"


def test_delete_writes_a_deleted_edit_and_keeps_the_terms_row(terms_conn):
    """人工删除不可被 ETL 恢复——terms 行仍然存在（ETL 还在维护它），
    但对所有读路径不可见。"""
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="待人工删除", aliases=[], term_type="t"
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient(edge_count=0)
    try:
        client = TestClient(app)
        response = client.delete(
            "/api/admin/t1/terms/t:待人工删除", headers=_authed_headers(session_store)
        )
        assert response.status_code == 200

        # terms 表那一行仍然存在——DELETE 不删 terms 行。
        raw_terms = asyncio.run(list_terms(terms_conn, "t1"))
        assert [t.standard_name for t in raw_terms] == ["待人工删除"]

        edits = asyncio.run(list_term_edits_for_node_key(terms_conn, "t1", "t:待人工删除"))
        assert FIELD_DELETED in edits

        # 读端点看不到它。
        list_response = client.get("/api/admin/t1/terms", headers=_authed_headers(session_store))
    finally:
        app.dependency_overrides.clear()

    assert list_response.json()["terms"] == []


def test_post_writes_a_created_edit_not_a_terms_row(terms_conn):
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
                "standard_name": "纯编辑层实体", "aliases": ["别名A"], "term_type": "t",
                "extra_properties": {},
            },
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 200

        # terms 表没有新增行——POST 只写 __created__ 编辑。
        raw_terms = asyncio.run(list_terms(terms_conn, "t1"))
        assert raw_terms == []

        edits = asyncio.run(
            list_term_edits_for_node_key(terms_conn, "t1", "t:纯编辑层实体")
        )
        created = edits[FIELD_CREATED]
        assert created["standard_name"] == "纯编辑层实体"
        assert created["term_type"] == "t"
        assert created["aliases"] == ["别名A"]
        assert created["extra_properties"] == {}

        # 读端点能看到这个新实体。
        list_response = client.get("/api/admin/t1/terms", headers=_authed_headers(session_store))
    finally:
        app.dependency_overrides.clear()

    node_keys = {t["node_key"] for t in list_response.json()["terms"]}
    assert "t:纯编辑层实体" in node_keys


def test_put_only_writes_edits_for_the_fields_actually_submitted(terms_conn):
    """字段级而不是整行级。payload 里 extra_properties 缺席时不写任何
    属性编辑——否则该实体的属性值再也不跟着数据源更新。"""
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
    asyncio.run(
        create_term_type(
            terms_conn, tenant_id="t1", value="订单号2",
            extra_fields=[ExtraFieldSpec(name="revenue", value_type="number")],
        )
    )
    asyncio.run(confirm_ontology(terms_conn, "t1"))
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="订单X", aliases=[],
            term_type="订单号2", extra_properties={"revenue": 100.0},
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/t1/terms/订单号2:订单X",
            json={"standard_name": "订单X改", "aliases": [], "term_type": "订单号2"},
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()

    edits = asyncio.run(list_term_edits_for_node_key(terms_conn, "t1", "订单号2:订单X"))
    assert not any(field.startswith(EXTRA_PROPERTY_PREFIX) for field in edits)
    assert FIELD_EXTRA_PROPERTIES not in edits
    assert edits["standard_name"] == "订单X改"


def test_create_term_with_existing_node_key_and_orphaned_property_succeeds(terms_conn):
    """Task 4 Fix：POST 现在写 __created__ 编辑，当 node_key 撞上 terms 表已有的行时，
    实际上是在编辑该行。如果该行有已废弃的属性键（在当前 term_type 声明中不存在），
    新的 __created__ 编辑中也带有这个键时，应该豁免字段名校验（祖父豁免），
    允许该编辑成功。

    场景：
    1. terms 表中已有行，带有属性 "orphaned_key"
    2. term_type 的当前声明不包含 "orphaned_key"（已废弃）
    3. POST 同一个 node_key，payload 也带 {"orphaned_key": "value"}
    4. 应该成功（200），而不是因为未知字段返回 400
    """
    import json
    # term_type "t" 在前面的 fixture 中已经创建，且不声明任何属性
    # 直接用低层 SQL 插入一条带有 orphaned_key 的实体，绕过校验
    asyncio.run(
        terms_conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t1", "t:既有术语", "既有术语", json.dumps(["别名1"]), "t",
             json.dumps({"orphaned_key": "old_value"}), "manual"),
        )
    )
    asyncio.run(terms_conn.commit())

    session_store = AdminSessionStore()
    graph_client = SpyGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        # 用同一个 node_key POST，payload 也带这个已废弃的属性
        response = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "既有术语",
                "aliases": ["别名1"],
                "term_type": "t",
                "extra_properties": {"orphaned_key": "new_value"},
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    # 修复后应该成功（200），表示祖父豁免已应用
    assert response.status_code == 200
    # 响应体应该反映 payload 中的属性值
    assert response.json()["extra_properties"] == {"orphaned_key": "new_value"}


def test_post_on_a_manually_deleted_node_key_resurrects_it(terms_conn):
    """人工重建一个曾被人工删除的 node_key，应当让它重新可见。

    "人工删除不可被恢复"这条规矩的准确表述是 Foundry 的
    「Deletions aren't reversible by datasource updates」——禁的是**数据源
    更新**把人删掉的东西带回来（ETL 重跑仍然做不到），不是禁止人自己撤销
    自己的删除。

    修之前这条路径不是静默成功，而是 500：POST 写完 __created__ 之后紧接着
    调 get_term_merged_by_node_key 同步图谱，而它对被 __deleted__ 标记的实体
    抛 TermNotFoundError，路由没有捕获——一次合法的重建变成不透明的服务端
    错误。
    """
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="删了又建", aliases=[], term_type="t"
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient(edge_count=0)
    try:
        client = TestClient(app)
        headers = _authed_headers(session_store)

        deleted = client.delete("/api/admin/t1/terms/t:删了又建", headers=headers)
        assert deleted.status_code == 200
        assert client.get("/api/admin/t1/terms", headers=headers).json()["terms"] == []

        recreated = client.post(
            "/api/admin/t1/terms",
            json={
                "standard_name": "删了又建", "aliases": [], "term_type": "t",
                "extra_properties": {},
            },
            headers=headers,
        )
        assert recreated.status_code == 200

        listed = client.get("/api/admin/t1/terms", headers=headers).json()["terms"]
    finally:
        app.dependency_overrides.clear()

    # 重新可见了。
    assert [t["standard_name"] for t in listed] == ["删了又建"]
    # __deleted__ 编辑已经被撤掉，不是靠别的方式绕过去的。
    edits = asyncio.run(list_term_edits_for_node_key(terms_conn, "t1", "t:删了又建"))
    assert FIELD_DELETED not in edits


def test_search_matches_standard_name_and_aliases(terms_conn):
    """搜索命中标准名或任一别名，不区分大小写。"""
    for name, aliases in (("Coca-Cola", ["可乐"]), ("Pepsi", []), ("矿泉水", [])):
        asyncio.run(
            create_term(terms_conn, tenant_id="t1", standard_name=name, aliases=aliases, term_type="t")
        )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        headers = _authed_headers(session_store)

        by_name = client.get("/api/admin/t1/terms?page=1&page_size=20&q=cola", headers=headers).json()
        by_alias = client.get("/api/admin/t1/terms?page=1&page_size=20&q=可乐", headers=headers).json()
        miss = client.get("/api/admin/t1/terms?page=1&page_size=20&q=不存在的东西", headers=headers).json()
    finally:
        app.dependency_overrides.clear()

    # 大小写不敏感：查 "cola" 命中 "Coca-Cola"。
    assert [t["standard_name"] for t in by_name["terms"]] == ["Coca-Cola"]
    assert by_name["total"] == 1
    # 别名命中。
    assert [t["standard_name"] for t in by_alias["terms"]] == ["Coca-Cola"]
    # total 是命中数，不是全表数——否则分页器会撒谎。
    assert miss["total"] == 0
    assert miss["terms"] == []


def test_search_finds_a_manually_renamed_term_by_its_new_name(terms_conn):
    """**这条是搜索最容易做错的地方。**

    搜索必须作用在合并视图（terms + 人工编辑）上。如果在 SQL 里按 terms 表
    的原始值过滤，人工改过展示名的术语就只能用**旧**名字搜到、用界面上看到
    的新名字反而搜不到——正好反了。
    """
    asyncio.run(
        create_term(
            terms_conn, tenant_id="t1", standard_name="管道产出的名字",
            aliases=[], term_type="t",
        )
    )
    asyncio.run(
        upsert_term_edit(
            terms_conn, tenant_id="t1", node_key="t:管道产出的名字",
            field="standard_name", value="人工改过的名字", edited_by="admin",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        headers = _authed_headers(session_store)
        by_new = client.get("/api/admin/t1/terms?page=1&page_size=20&q=人工改过", headers=headers).json()
        by_old = client.get("/api/admin/t1/terms?page=1&page_size=20&q=管道产出", headers=headers).json()
    finally:
        app.dependency_overrides.clear()

    # 用界面上看到的新名字能搜到。
    assert [t["standard_name"] for t in by_new["terms"]] == ["人工改过的名字"]
    # 用已经不再显示的旧名字搜不到——搜索和列表看到的是同一份数据。
    assert by_old["terms"] == []


def test_pager_total_matches_the_merged_list_not_the_raw_table(terms_conn):
    """分页器的 total 必须跟列表内容同口径。

    count_terms 数的是 terms 原始表，跟 list_terms_merged 返回的内容对不上：
    人工删除（__deleted__）的行仍在表里但不出现在列表中，纯编辑层创建
    （__created__ 且 terms 无对应行）的实体则相反。

    两个偏差方向相反、会部分抵消——这比单纯多算更坏：抵消会让问题在小数据上
    看着"差不多对"，掩盖两个独立的错误。这条用例同时构造两种，确保修复不是
    靠抵消蒙对的。
    """
    for i in range(5):
        asyncio.run(
            create_term(
                terms_conn, tenant_id="t1", standard_name=f"n{i}", aliases=[], term_type="t"
            )
        )
    # 删掉两条（terms 行还在，但列表里不该出现）
    for i in (0, 1):
        asyncio.run(
            upsert_term_edit(
                terms_conn, tenant_id="t1", node_key=f"t:n{i}",
                field=FIELD_DELETED, value=None, edited_by="admin",
            )
        )
    # 纯编辑层创建一条（terms 里没有，但列表里该出现）
    asyncio.run(
        upsert_term_edit(
            terms_conn, tenant_id="t1", node_key="t:纯编辑层",
            field=FIELD_CREATED,
            value={
                "standard_name": "纯编辑层", "term_type": "t",
                "aliases": [], "extra_properties": {},
            },
            edited_by="admin",
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: SpyGraphClient()
    try:
        client = TestClient(app)
        body = client.get(
            "/api/admin/t1/terms?page=1&page_size=100", headers=_authed_headers(session_store)
        ).json()
    finally:
        app.dependency_overrides.clear()

    # 5 条原始 - 2 条删除 + 1 条编辑层创建 = 4 条
    assert len(body["terms"]) == 4
    # total 必须等于实际列出的条数，而不是原始表的 5。
    assert body["total"] == 4


def _relation(direction: str, relation_type: str, standard_name: str) -> dict:
    return {
        "direction": direction,
        "relation_type": relation_type,
        "node_key": f"t:{standard_name}",
        "standard_name": standard_name,
        "term_type": "t",
    }


def _delete_blocked_term(terms_conn, graph_client) -> "object":
    asyncio.run(
        create_term(terms_conn, tenant_id="t1", standard_name="使用中", aliases=[], term_type="t")
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        return client.delete(
            "/api/admin/t1/terms/t:使用中", headers=_authed_headers(session_store)
        )
    finally:
        app.dependency_overrides.clear()


def test_delete_blocked_term_names_the_edges_in_the_way(terms_conn):
    """"该术语已在图谱中使用"不告诉用户是哪条边挡路，而后台又没有别的地方
    能查出来——服务端此刻手里就有这份明细。方向要照实渲染：入边写成
    「对端 -类型-> 本术语」，反过来写会把关系的角色说反。"""
    graph_client = SpyGraphClient(
        edge_count=2,
        relations=[
            _relation("in", "RELATED_TO", "错误码E502"),
            _relation("out", "PART_OF", "登录域"),
        ],
    )

    response = _delete_blocked_term(terms_conn, graph_client)

    assert response.status_code == 409
    body = response.json()
    assert "错误码E502 -RELATED_TO-> 使用中" in body["detail"]
    assert "使用中 -PART_OF-> 登录域" in body["detail"]
    assert "2 条" in body["detail"]
    assert body["blocking_relations"]["total"] == 2
    assert [
        (edge["direction"], edge["relation_type"], edge["node_key"])
        for edge in body["blocking_relations"]["edges"]
    ] == [("in", "RELATED_TO", "t:错误码E502"), ("out", "PART_OF", "t:登录域")]


def test_delete_blocked_term_lists_a_few_edges_and_still_reports_the_total(terms_conn):
    """点名前几条 + 总数兜底，跟删分类那条（d2f1197）同构。第 4 条之后的
    对端名字不该出现在消息里，否则消息长到没人读。"""
    graph_client = SpyGraphClient(
        edge_count=5,
        relations=[_relation("out", "RELATED_TO", f"邻居{i}") for i in range(5)],
    )

    response = _delete_blocked_term(terms_conn, graph_client)

    body = response.json()
    assert response.status_code == 409
    assert "等共 5 条" in body["detail"]
    assert "邻居3" not in body["detail"]
    assert "邻居4" not in body["detail"]
    assert [edge["standard_name"] for edge in body["blocking_relations"]["edges"]] == [
        "邻居0", "邻居1", "邻居2",
    ]
    # 结构化字段里的 total 是"总共几条"，不是"列出了几条"——前端拿它显示
    # 还剩多少没处理，退化成样本条数就等于把总数悄悄说小了。
    assert body["blocking_relations"]["total"] == 5


def test_delete_blocked_term_still_reports_the_count_when_edge_lookup_fails(terms_conn):
    """取明细失败不能把 409 变成 500：守卫的结论（有边、有几条）已经拿到了，
    丢掉它反而让用户连"为什么删不掉"都不知道。"""
    graph_client = SpyGraphClient(edge_count=2, relations_error=RuntimeError("Neo4j 挂了"))

    response = _delete_blocked_term(terms_conn, graph_client)

    assert response.status_code == 409
    body = response.json()
    assert "2 条" in body["detail"]
    assert body["blocking_relations"]["edges"] == []


def _delete_relation(terms_conn, graph_client, query: str):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        return client.delete(
            f"/api/admin/t1/terms/t:使用中/relations?{query}",
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()


def test_delete_outgoing_relation_edge_keeps_the_term_as_the_subject(terms_conn):
    """direction=out 表示"这个术语指向对端"，主语是它自己。方向映射反了
    会删掉另一条边——对同一对术语之间的双向关系来说，那正是用户没点的
    那一条。"""
    graph_client = SpyGraphClient(removed_edges=1)

    response = _delete_relation(
        terms_conn, graph_client,
        "direction=out&relation_type=PART_OF&other_node_key=t:登录域",
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert graph_client.deleted_edges == [
        {
            "tenant_id": "t1",
            "subject_node_key": "t:使用中",
            "relation_type": "PART_OF",
            "object_node_key": "t:登录域",
        }
    ]


def test_delete_incoming_relation_edge_makes_the_other_term_the_subject(terms_conn):
    graph_client = SpyGraphClient(removed_edges=1)

    response = _delete_relation(
        terms_conn, graph_client,
        "direction=in&relation_type=RELATED_TO&other_node_key=t:错误码E502",
    )

    assert response.status_code == 200
    assert graph_client.deleted_edges == [
        {
            "tenant_id": "t1",
            "subject_node_key": "t:错误码E502",
            "relation_type": "RELATED_TO",
            "object_node_key": "t:使用中",
        }
    ]


def test_delete_relation_edge_that_matched_nothing_returns_404(terms_conn):
    """一条都没删掉却回 200，用户刷新后那条边还在——静默失败。图客户端
    如实返回 0，接口就必须把它翻译成一个用户看得见的错误。"""
    graph_client = SpyGraphClient(removed_edges=0)

    response = _delete_relation(
        terms_conn, graph_client,
        "direction=out&relation_type=RELATED_TO&other_node_key=t:不存在",
    )

    assert response.status_code == 404
    assert "RELATED_TO" in response.json()["detail"]


def test_delete_relation_edge_rejects_unknown_direction(terms_conn):
    graph_client = SpyGraphClient(removed_edges=1)

    response = _delete_relation(
        terms_conn, graph_client,
        "direction=sideways&relation_type=RELATED_TO&other_node_key=t:x",
    )

    assert response.status_code == 422
    assert graph_client.deleted_edges == []


_MISMATCHED_EDGE = {
    "direction": "out", "relation_type": "RELATED_TO", "node_key": "t:登录模块",
    "standard_name": "登录模块", "term_type": "t",
    "other_tenant_id": "t1", "edge_tenant_id": "demo",
}
_CROSS_TENANT_EDGE = {
    "direction": "in", "relation_type": "PART_OF", "node_key": "t:别家的实体",
    "standard_name": "别家的实体", "term_type": "t",
    "other_tenant_id": "tenant_b", "edge_tenant_id": "tenant_b",
}


def _call_inconsistent(terms_conn, graph_client, *, method: str, query: str = "", headers=None):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: terms_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        url = "/api/admin/t1/terms/t:使用中/relations/inconsistent"
        if query:
            url = f"{url}?{query}"
        auth = (headers or _authed_headers)(session_store)
        if method == "GET":
            return client.get(url, headers=auth)
        return client.delete(url, headers=auth)
    finally:
        app.dependency_overrides.clear()


def test_list_inconsistent_relations_separates_the_two_dirty_classes(terms_conn):
    """两类脏边要分开说：边的 tenant_id 标错了（本租户内部的历史遗留），
    和两端节点分属不同租户（隔离本身已经破了）。处理方式不同，界面上
    也得让用户看出区别。"""
    graph_client = SpyGraphClient(inconsistent=[_MISMATCHED_EDGE, _CROSS_TENANT_EDGE])

    response = _call_inconsistent(terms_conn, graph_client, method="GET")

    assert response.status_code == 200
    rows = response.json()["relations"]
    assert [r["category"] for r in rows] == ["edge_tenant_mismatch", "cross_tenant"]
    assert rows[0]["node_key"] == "t:登录模块"
    assert rows[0]["edge_tenant_id"] == "demo"
    assert graph_client.listed_inconsistent == [{"tenant_id": "t1", "node_key": "t:使用中"}]


def test_member_is_not_shown_the_other_tenants_identity_on_a_cross_tenant_edge(terms_conn):
    """跨租户的边上，对端节点是另一个租户的数据。member 需要知道"这里挂着
    一条跨租户的边、得找平台管理员"，但不需要、也不该知道对面那个实体叫
    什么、属于哪个租户。"""
    graph_client = SpyGraphClient(inconsistent=[_CROSS_TENANT_EDGE])

    response = _call_inconsistent(
        terms_conn, graph_client, method="GET", headers=_member_headers
    )

    assert response.status_code == 200
    assert "别家的实体" not in response.text
    assert "tenant_b" not in response.text
    row = response.json()["relations"][0]
    assert row["category"] == "cross_tenant"
    assert row["node_key"] is None
    assert row["standard_name"] is None
    assert row["other_tenant_id"] is None
    assert row["deletable"] is False
    # 关系类型和方向保留：那说的是本租户自己节点身上挂着什么，不是对面的信息
    assert row["relation_type"] == "PART_OF"


def test_admin_sees_the_whole_cross_tenant_edge(terms_conn):
    """平台管理员是唯一有权处理这类边的人，看不到两端是谁就无从判断。"""
    graph_client = SpyGraphClient(inconsistent=[_CROSS_TENANT_EDGE])

    response = _call_inconsistent(terms_conn, graph_client, method="GET")

    row = response.json()["relations"][0]
    assert row["node_key"] == "t:别家的实体"
    assert row["other_tenant_id"] == "tenant_b"
    assert row["deletable"] is True


def test_member_can_delete_an_edge_whose_tenant_mark_is_wrong(terms_conn):
    """两端节点都在自己租户里、只有边标错了租户——这条边挡着 member 自己
    的实体删除，判据（两端节点的租户）也完全落在他有权的范围内，他就该
    能删掉它。起点租户一律取 URL 里那个已经过 require_tenant_access 校验的
    租户，不是请求里自报的值。"""
    graph_client = SpyGraphClient(removed_edges=1)

    response = _call_inconsistent(
        terms_conn, graph_client, method="DELETE", headers=_member_headers,
        query="direction=out&relation_type=RELATED_TO&other_node_key=t:登录模块&other_tenant_id=t1",
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert graph_client.deleted_inconsistent_edges == [
        {
            "subject_tenant_id": "t1", "subject_node_key": "t:使用中",
            "relation_type": "RELATED_TO",
            "object_tenant_id": "t1", "object_node_key": "t:登录模块",
        }
    ]


def test_member_cannot_delete_a_cross_tenant_edge(terms_conn):
    """两端节点分属不同租户时，删掉这条边同时改变了另一个租户的图谱。
    member 只对自己那一个租户有权，不能单方面替对面做这个决定——这里
    必须挡住，而且一条都不能落到图客户端上。"""
    graph_client = SpyGraphClient(removed_edges=1)

    response = _call_inconsistent(
        terms_conn, graph_client, method="DELETE", headers=_member_headers,
        query="direction=in&relation_type=PART_OF&other_node_key=t:别家的实体&other_tenant_id=tenant_b",
    )

    assert response.status_code == 403
    assert graph_client.deleted_inconsistent_edges == []
    assert "管理员" in response.json()["detail"]


def test_admin_can_delete_a_cross_tenant_edge(terms_conn):
    """平台管理员对两个租户都有权，由他来做这个决定。direction=in 表示
    对端才是主语，两端的租户必须跟着方向一起翻。"""
    graph_client = SpyGraphClient(removed_edges=1)

    response = _call_inconsistent(
        terms_conn, graph_client, method="DELETE",
        query="direction=in&relation_type=PART_OF&other_node_key=t:别家的实体&other_tenant_id=tenant_b",
    )

    assert response.status_code == 200
    assert graph_client.deleted_inconsistent_edges == [
        {
            "subject_tenant_id": "tenant_b", "subject_node_key": "t:别家的实体",
            "relation_type": "PART_OF",
            "object_tenant_id": "t1", "object_node_key": "t:使用中",
        }
    ]


def test_delete_inconsistent_relation_that_matched_nothing_returns_404(terms_conn):
    """一条都没删掉却回 200 就是静默失败：用户刷新后那条边还在，没有任何
    地方告诉他删的不是它。"""
    graph_client = SpyGraphClient(removed_edges=0)

    response = _call_inconsistent(
        terms_conn, graph_client, method="DELETE",
        query="direction=out&relation_type=RELATED_TO&other_node_key=t:不存在&other_tenant_id=t1",
    )

    assert response.status_code == 404
    assert "RELATED_TO" in response.json()["detail"]


def test_the_subject_side_tenant_always_comes_from_the_url_not_from_the_request(terms_conn):
    """起点侧的租户固定取 URL 里那个（已经过 require_tenant_access 校验），
    绝不能取请求里自报的 other_tenant_id——否则谁都能把两端都指向别人的
    租户，这条路径就成了越权删边的入口。direction=out 时这两个值不同，
    正好能把它们区分开。"""
    graph_client = SpyGraphClient(removed_edges=1)

    response = _call_inconsistent(
        terms_conn, graph_client, method="DELETE",
        query="direction=out&relation_type=RELATED_TO&other_node_key=t:别家的实体&other_tenant_id=tenant_b",
    )

    assert response.status_code == 200
    assert graph_client.deleted_inconsistent_edges == [
        {
            "subject_tenant_id": "t1", "subject_node_key": "t:使用中",
            "relation_type": "RELATED_TO",
            "object_tenant_id": "tenant_b", "object_node_key": "t:别家的实体",
        }
    ]
