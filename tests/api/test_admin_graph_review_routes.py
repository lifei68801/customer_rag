import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.graphrag.ontology import Term
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.review_queue import enqueue_for_review, ensure_review_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_terms_schema(conn)
    # Task 3：approve/reject 路由现在经 list_terms_merged() 读术语表，测试
    # 连接要跟生产环境一样把 term_edits 表也建好，否则会报
    # "no such table: term_edits"。
    await ensure_term_edits_schema(conn)
    # Task 4：approve/reject 现在会先用 review_conn 调 require_active_tenant()
    # 校验 payload.tenant_id——真实的 deps.get_review_conn() 会自动建好
    # tenants 表并回填历史租户，这里是手工建表的测试连接，绕开了那条路径，
    # 必须显式建表 + 注册本文件所有用例用到的 tenant_id（"t1"）。
    await create_tenants_table(conn)
    await create_tenant(conn, tenant_id="t1", name="t1")
    # approve 路由现在还会查该租户 status="confirmed" 的关系类型/类型组合
    # 白名单（Fix 1：approve_review 补齐了跟 normalize_and_write_relations
    # 一样的"已确认本体范围"校验），这两张表也要建好，否则查询会报
    # "no such table"。
    await ensure_ontology_schema(conn)
    return conn


async def _seed_confirmed_ontology(
    conn: aiosqlite.Connection, *, tenant_id: str, relation_type: str,
    subject_term_type: str, object_term_type: str,
) -> None:
    """直接往 tenant_relation_types/term_type_relation_allowlist 插入
    status='confirmed' 的行——测试只关心 approve 路由能查到这条"已确认"
    数据，不需要走完整的草稿编辑+confirm_ontology 生命周期。"""
    await conn.execute(
        "INSERT INTO tenant_relation_types "
        "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
        "source, status) VALUES (?, ?, ?, '', 0, 'custom', 'confirmed')",
        (tenant_id, relation_type, relation_type),
    )
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, 'confirmed')",
        (tenant_id, subject_term_type, relation_type, object_term_type),
    )
    await conn.commit()


@pytest.fixture
def review_conn():
    """审核队列库连接。必须显式 close：aiosqlite 的后台工作线程不是 daemon
    线程，泄漏一个未关闭的连接会让 pytest 进程在跑完全部用例后卡在解释器
    退出阶段（threading._shutdown 等这个线程），表现为"测试全绿但命令不返回"。
    做法同 test_admin_document_routes.py 的 ingestion_conn fixture。
    """
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def _seed_terms(conn: aiosqlite.Connection, terms: list[Term]) -> None:
    """approve 路由现在不再经 deps.get_terms（Fix 3：直接用 payload.tenant_id
    从 review_conn 查 terms 表），测试改为直接往 terms 表插行，绕开
    create_term() 的分类校验——这里只关心路由查到了正确的术语。
    """
    for term in terms:
        await conn.execute(
            "INSERT OR REPLACE INTO terms "
            "(tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
            (
                term.tenant_id,
                term.node_key,
                term.standard_name,
                json.dumps(term.aliases, ensure_ascii=False),
                term.term_type,
                json.dumps(term.extra_properties, ensure_ascii=False),
            ),
        )
    await conn.commit()


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(self, **kwargs) -> None:
        self.written.append(kwargs)


class UnavailableGraphClient:
    """模拟 Neo4j 挂掉：merge_relation 抛一个非业务异常。

    用 ConnectionError 而不是 ValueError——后者已经被路由当成"输入有问题"
    转成 400 了，用它测不到基础设施异常那条路径。
    """

    async def merge_relation(self, **kwargs) -> None:
        raise ConnectionError("Neo4j 不可用")


class RelationTypeRejectingGraphClient:
    async def merge_relation(self, **kwargs) -> None:
        raise ValueError(f"不允许的关系类型: {kwargs['relation_type']!r}")


def test_list_pending_reviews_returns_tenant_scoped_rows(review_conn):
    asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/graph-reviews", params={},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(response.json()["reviews"]) == 1


def test_approve_review_calls_graph_client_and_moves_to_history(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    graph_client = FakeGraphClient()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="t1", node_key="A", standard_name="A", aliases=[],
                    term_type="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="",
                ),
            ],
        )
    )
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="", object_term_type="",
        )
    )
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # 路由层内部用 datetime.now() 生成 recorded_at，测试跑的时刻不可预知
    # 具体值，只断言其它字段+provenance（走的是 human_approved 路径）。
    assert len(graph_client.written) == 1
    written = graph_client.written[0]
    assert written["subject_standard_name"] == "A"
    assert written["object_standard_name"] == "B"
    assert written["relation_type"] == "RELATED_TO"
    assert written["source"] == "s.md"
    assert written["tenant_id"] == "t1"
    assert written["provenance"] == "human_approved"

    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        history_response = TestClient(app)
        response = history_response.get(
            "/api/admin/t1/graph-reviews", params={"status": "approved"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()
    assert len(response.json()["reviews"]) == 1


def test_reject_review_marks_rejected(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/reject",
            json={"note": "噪声"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_approve_with_unknown_tenant_returns_404(review_conn):
    """Task 4：租户存在性校验要在审核队列的具体业务逻辑之前生效——一个
    从未在 tenants 注册表里登记过的 tenant_id，即使对应的 review_id 真实
    存在，也应该被挡在 404，而不是被当作合法租户继续往下走。"""
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/no-such-tenant/graph-reviews/{review_id}/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_approve_nonexistent_review_returns_404(review_conn):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/admin/t1/graph-reviews/999/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_list_reviews_without_session_token_returns_401(review_conn):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.get("/api/admin/t1/graph-reviews", params={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_approve_already_resolved_review_returns_409(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="t1", node_key="A", standard_name="A", aliases=[],
                    term_type="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="",
                ),
            ],
        )
    )
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="", object_term_type="",
        )
    )
    try:
        client = TestClient(app)
        payload = {"subject_standard_name": "A", "object_standard_name": "B"}
        first = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json=payload, headers=_authed_headers(session_store),
        )
        second = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json=payload, headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 409


def test_reject_already_resolved_review_returns_409(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        payload = {"note": "噪声"}
        first = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/reject",
            json=payload, headers=_authed_headers(session_store),
        )
        second = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/reject",
            json=payload, headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 409


def test_list_reviews_status_all_returns_both_approved_and_rejected(review_conn):
    approve_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    reject_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="c", object_candidate="d", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="t1", node_key="A", standard_name="A", aliases=[],
                    term_type="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="",
                ),
            ],
        )
    )
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="", object_term_type="",
        )
    )
    try:
        client = TestClient(app)
        client.post(
            f"/api/admin/t1/graph-reviews/{approve_id}/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
        client.post(
            f"/api/admin/t1/graph-reviews/{reject_id}/reject",
            json={},
            headers=_authed_headers(session_store),
        )
        response = client.get(
            "/api/admin/t1/graph-reviews", params={"status": "all"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(response.json()["reviews"]) == 2


def test_reject_review_shows_up_in_rejected_history(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        reject_response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/reject",
            json={"note": "噪声"},
            headers=_authed_headers(session_store),
        )
        history_response = client.get(
            "/api/admin/t1/graph-reviews", params={"status": "rejected"},
            headers=_authed_headers(session_store),
        )
        pending_response = client.get(
            "/api/admin/t1/graph-reviews", params={"status": "pending"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert reject_response.status_code == 200
    assert len(history_response.json()["reviews"]) == 1
    assert history_response.json()["reviews"][0]["resolved_note"] == "噪声"
    assert pending_response.json()["reviews"] == []


def test_list_pending_reviews_returns_total_count_and_respects_page_size(review_conn):
    for i in range(3):
        asyncio.run(
            enqueue_for_review(
                review_conn, subject_candidate=f"s{i}", object_candidate=f"o{i}",
                relation_type="RELATED_TO", reason="subject_unresolved",
                source="s.md", tenant_id="t1",
            )
        )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/graph-reviews",
            params={"status": "pending", "page": 1, "page_size": 2},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["reviews"]) == 2
    assert body["total"] == 3


def test_list_pending_reviews_second_page_returns_remaining_rows(review_conn):
    for i in range(3):
        asyncio.run(
            enqueue_for_review(
                review_conn, subject_candidate=f"s{i}", object_candidate=f"o{i}",
                relation_type="RELATED_TO", reason="subject_unresolved",
                source="s.md", tenant_id="t1",
            )
        )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/t1/graph-reviews",
            params={"status": "pending", "page": 2, "page_size": 2},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["reviews"]) == 1
    assert body["reviews"][0]["subject_candidate"] == "s2"
    assert body["total"] == 3


def test_approve_review_with_invalid_relation_type_returns_400(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b",
            relation_type="不存在的关系", reason="invalid_relation_type",
            source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: RelationTypeRejectingGraphClient()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="t1", node_key="A", standard_name="A", aliases=[],
                    term_type="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="",
                ),
            ],
        )
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_approve_review_with_relation_type_not_in_confirmed_ontology_returns_400(review_conn):
    """Fix 1 回归测试：relation_type/类型组合两侧都合法对齐了术语表，但
    不在该租户已确认的本体范围内——approve 路由要挡住，不能直接写图谱。
    这里刻意不调用 _seed_confirmed_ontology，模拟该租户还没有确认任何
    关系类型/类型组合的场景。"""
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="not_in_confirmed_ontology", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    graph_client = FakeGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="t1", node_key="A", standard_name="A", aliases=[],
                    term_type="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="",
                ),
            ],
        )
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert graph_client.written == []


def test_approve_review_with_standard_name_not_in_terms_returns_400(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="",
                ),
            ],
        )
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


def test_approve_review_accepts_optional_term_type_hints(review_conn):
    """Task 4：请求体新增的 subject_term_type/object_term_type 是可选字段，
    加了之后请求依然成功（不报 422）；这里同时验证它们真的被透传给
    approve_review 并生效——两个同名不同类型的术语（"Coffee" 同时存在
    "产品"/"类目" 两种类型）如果不传类型提示会因为 standard_name 有歧义
    被拒绝（见 test_review_queue.py 的
    test_approve_review_rejects_ambiguous_standard_name_without_hint），
    传了类型提示之后应该精确解析到对应类型的那一条，写入图谱时用它的
    node_key。"""
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="Coffee", object_candidate="Coffee",
            relation_type="PART_OF", reason="fuzzy_match_needs_confirmation",
            source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    graph_client = FakeGraphClient()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(
                    tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee",
                    aliases=[], term_type="产品",
                ),
                Term(
                    tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee",
                    aliases=[], term_type="类目",
                ),
            ],
        )
    )
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="PART_OF",
            subject_term_type="产品", object_term_type="类目",
        )
    )
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json={
                "subject_standard_name": "Coffee",
                "object_standard_name": "Coffee",
                "subject_term_type": "产品",
                "object_term_type": "类目",
            },
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(graph_client.written) == 1
    assert graph_client.written[0]["subject_standard_name"] == "产品:Coffee"
    assert graph_client.written[0]["object_standard_name"] == "类目:Coffee"


def test_approve_review_with_ambiguous_standard_name_message_mentions_candidate_types(review_conn):
    """Task 2：错误消息应该明确提示"存在歧义"和候选类型列表，而不是
    笼统的"不在术语表中"——见 test_review_queue.py 里对
    _standard_name_not_found_message 的单元测试。这里在 API 层再验证一次
    是因为这条消息是直接透传给前端展示的（GraphReviewsPage.tsx 的
    error 状态），值得确认它没有在 HTTPException 这一层被吞掉或改写。"""
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="Coffee", object_candidate="B",
            relation_type="RELATED_TO", reason="fuzzy_match_needs_confirmation",
            source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee", aliases=[], term_type="产品"),
                Term(tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee", aliases=[], term_type="类目"),
                Term(tenant_id="t1", node_key="B", standard_name="B", aliases=[], term_type=""),
            ],
        )
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json={"subject_standard_name": "Coffee", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "存在歧义" in response.json()["detail"]


def test_approve_returns_503_and_keeps_the_review_pending_when_graph_is_down(review_conn):
    """图谱写入失败时返回 503（可重试），不是 500（不透明），且记录仍停在
    待审队列。

    503 而不是 500 是有意的：审核员看到 500 分不出"我填错了"还是"图谱挂了"，
    而这两种的应对完全不同。批量批准时尤其明显——图谱挂掉会让 10 条连续
    失败，用户需要知道该等一等再整批重试。

    记录停在 pending 是 approve_review 的写入顺序保证的：先写图谱、后改
    状态，图谱这一步抛异常时那条 UPDATE 根本没执行。这个方向可自愈——
    反过来（先改状态后写图）会留下"记录已批准、图谱没有边"的不可自愈状态。
    """
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    asyncio.run(
        _seed_terms(
            review_conn,
            [
                Term(tenant_id="t1", node_key="A", standard_name="A", aliases=[], term_type=""),
                Term(tenant_id="t1", node_key="B", standard_name="B", aliases=[], term_type=""),
            ],
        )
    )
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="", object_term_type="",
        )
    )
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: UnavailableGraphClient()
    try:
        client = TestClient(app)
        headers = _authed_headers(session_store)
        response = client.post(
            f"/api/admin/t1/graph-reviews/{review_id}/approve",
            json={"subject_standard_name": "A", "object_standard_name": "B"},
            headers=headers,
        )
        # 记录必须还在待审列表里——否则用户重试无门。
        pending = client.get(
            "/api/admin/t1/graph-reviews", params={}, headers=headers
        ).json()
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "稍后重试" in response.json()["detail"]
    assert [r["review_id"] for r in pending["reviews"]] == [review_id]
