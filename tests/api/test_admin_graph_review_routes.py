import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.graphrag.ontology import Term
from app.graphrag.review_queue import enqueue_for_review, ensure_review_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.terms_store import ensure_terms_schema
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


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_terms_schema(conn)
    # Task 4：approve/reject 现在会先用 review_conn 调 require_active_tenant()
    # 校验 payload.tenant_id——真实的 deps.get_review_conn() 会自动建好
    # tenants 表并回填历史租户，这里是手工建表的测试连接，绕开了那条路径，
    # 必须显式建表 + 注册本文件所有用例用到的 tenant_id（"t1"）。
    await create_tenants_table(conn)
    await create_tenant(conn, tenant_id="t1", name="t1")
    return conn


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
            "(tenant_id, node_key, standard_name, aliases, term_type, product_line, "
            "extra_properties) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                term.tenant_id,
                term.node_key,
                term.standard_name,
                json.dumps(term.aliases, ensure_ascii=False),
                term.term_type,
                term.product_line,
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
            "/api/admin/graph-reviews", params={"tenant_id": "t1"},
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
                    term_type="", product_line="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="", product_line="",
                ),
            ],
        )
    )
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/graph-reviews/{review_id}/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
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
            "/api/admin/graph-reviews", params={"tenant_id": "t1", "status": "approved"},
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
            f"/api/admin/graph-reviews/{review_id}/reject",
            json={"tenant_id": "t1", "note": "噪声"},
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
            f"/api/admin/graph-reviews/{review_id}/approve",
            json={
                "tenant_id": "no-such-tenant",
                "subject_standard_name": "A",
                "object_standard_name": "B",
            },
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
            "/api/admin/graph-reviews/999/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
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
        response = client.get("/api/admin/graph-reviews", params={"tenant_id": "t1"})
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
                    term_type="", product_line="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="", product_line="",
                ),
            ],
        )
    )
    try:
        client = TestClient(app)
        payload = {
            "tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B",
        }
        first = client.post(
            f"/api/admin/graph-reviews/{review_id}/approve",
            json=payload, headers=_authed_headers(session_store),
        )
        second = client.post(
            f"/api/admin/graph-reviews/{review_id}/approve",
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
        payload = {"tenant_id": "t1", "note": "噪声"}
        first = client.post(
            f"/api/admin/graph-reviews/{review_id}/reject",
            json=payload, headers=_authed_headers(session_store),
        )
        second = client.post(
            f"/api/admin/graph-reviews/{review_id}/reject",
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
                    term_type="", product_line="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="", product_line="",
                ),
            ],
        )
    )
    try:
        client = TestClient(app)
        client.post(
            f"/api/admin/graph-reviews/{approve_id}/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
        client.post(
            f"/api/admin/graph-reviews/{reject_id}/reject",
            json={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
        response = client.get(
            "/api/admin/graph-reviews", params={"tenant_id": "t1", "status": "all"},
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
            f"/api/admin/graph-reviews/{review_id}/reject",
            json={"tenant_id": "t1", "note": "噪声"},
            headers=_authed_headers(session_store),
        )
        history_response = client.get(
            "/api/admin/graph-reviews", params={"tenant_id": "t1", "status": "rejected"},
            headers=_authed_headers(session_store),
        )
        pending_response = client.get(
            "/api/admin/graph-reviews", params={"tenant_id": "t1", "status": "pending"},
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
            "/api/admin/graph-reviews",
            params={"tenant_id": "t1", "status": "pending", "page": 1, "page_size": 2},
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
            "/api/admin/graph-reviews",
            params={"tenant_id": "t1", "status": "pending", "page": 2, "page_size": 2},
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
                    term_type="", product_line="",
                ),
                Term(
                    tenant_id="t1", node_key="B", standard_name="B", aliases=[],
                    term_type="", product_line="",
                ),
            ],
        )
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/graph-reviews/{review_id}/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


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
                    term_type="", product_line="",
                ),
            ],
        )
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/graph-reviews/{review_id}/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
