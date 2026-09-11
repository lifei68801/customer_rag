import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.graphrag.ontology import Term
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.review_queue import (
    count_pending_reviews,
    enqueue_for_review,
    ensure_review_schema,
)
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import (
    FIELD_DELETED,
    ensure_term_edits_schema,
    upsert_term_edit,
)
from app.graphrag.terms_store import ensure_terms_schema, list_terms_merged
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
    # require_admin_session 现在每个请求都要确认账号仍是 active，
    # 所以本体库里必须有这张表和一个可用的管理员。
    await ensure_admin_users_schema(conn)
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
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
    # 分类也要播成 confirmed：create_term 的分类校验查的是 confirmed
    # （terms_store.py:795），create-missing-term 端点跟它同口径。此前这个
    # helper 只播了关系类型和组合，没播分类——approve 路由不查分类，所以
    # 一直没露出来。
    for value in {subject_term_type, object_term_type}:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types (tenant_id, value, status) "
            "VALUES (?, ?, 'confirmed')",
            (tenant_id, value),
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
    token = session_store.create_session(username="admin", role="admin", tenant_id=None)
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
    # 键名是 *_node_key：merge_relation 收的一直是 node_key（ADR-0003），
    # 2026-09-12 把参数名改成跟值一致。
    assert written["subject_node_key"] == "A"
    assert written["object_node_key"] == "B"
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
    # 这两条恰好是最能说明改名理由的断言：传进去的展示名都叫 Coffee，
    # 写进图里的是两个不同的 node_key。参数名叫 *_standard_name 的时候，
    # 这行断言读起来像在说"标准名是 产品:Coffee"，而那不是标准名。
    assert graph_client.written[0]["subject_node_key"] == "产品:Coffee"
    assert graph_client.written[0]["object_node_key"] == "类目:Coffee"


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


# ---------------------------------------------------------------------------
# 四个分页
#
# 审核员面对这四类要做的事完全不同：fuzzy 是确认一个候选，unresolved 是给
# 那一端建个实体，out_of_ontology 是决定要不要放宽本体，bad_type 是改关系
# 类型。此前它们共用同一对「批准/驳回」按钮，混在一屏里。
# ---------------------------------------------------------------------------


def _seed_reason(conn: aiosqlite.Connection, reason: str, *, tenant_id: str = "t1") -> None:
    asyncio.run(
        enqueue_for_review(
            conn, subject_candidate=f"s-{reason}", object_candidate="b",
            relation_type="RELATED_TO", reason=reason, source="s.md", tenant_id=tenant_id,
        )
    )


def _get(review_conn: aiosqlite.Connection, path: str, **params):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        return client.get(
            f"/api/admin/t1/graph-reviews{path}", params=params,
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()


def _seed_all_five(review_conn: aiosqlite.Connection) -> None:
    for reason in (
        "fuzzy_match_needs_confirmation",
        "subject_unresolved",
        "object_unresolved",
        "not_in_confirmed_ontology",
        "invalid_relation_type",
    ):
        _seed_reason(review_conn, reason)


def test_each_tab_returns_only_its_own_reasons(review_conn):
    """五种 reason 各造一条，逐个 tab 断言它拿到的正是自己那几种。

    这是整个拆分的核心断言。五种全造齐而不是只造要测的那一种——只造一种的
    话，「根本没过滤」的实现在每个 tab 上都能返回那一条，照样全绿。
    """
    _seed_all_five(review_conn)

    for tab, expected in (
        ("fuzzy", ["fuzzy_match_needs_confirmation"]),
        ("unresolved", ["object_unresolved", "subject_unresolved"]),
        ("out_of_ontology", ["not_in_confirmed_ontology"]),
        ("bad_type", ["invalid_relation_type"]),
    ):
        body = _get(review_conn, "", tab=tab).json()
        assert sorted(r["reason"] for r in body["reviews"]) == expected, tab
        # total 也要跟着 tab 走。不跟的话分页器按 5 条算，用户翻到第二页
        # 看到的是空的，而他以为那里还有东西。
        assert body["total"] == len(expected), tab


def test_the_unresolved_tab_collects_both_sides(review_conn):
    """subject_unresolved 和 object_unresolved 都归「一端对不上」。

    只收一种的话，另一半待办会消失在界面上——队列里还在，但没有任何页面
    列它，审核员永远不知道有这些东西。
    """
    _seed_reason(review_conn, "subject_unresolved")
    _seed_reason(review_conn, "object_unresolved")

    body = _get(review_conn, "", tab="unresolved").json()

    assert sorted(r["reason"] for r in body["reviews"]) == [
        "object_unresolved",
        "subject_unresolved",
    ]


def test_no_tab_still_returns_everything(review_conn):
    """不传 tab 时行为不变——既有前端和用例不传这个参数。"""
    _seed_all_five(review_conn)

    body = _get(review_conn, "").json()

    assert body["total"] == 5


def test_an_unknown_tab_name_is_a_400_not_an_empty_list(review_conn):
    """拼错 tab 名返回 400 而不是空列表。

    返回空的话，前端拼错一个字母就得到一个永远空着的分页，而它看起来完全
    正常——「这一类没有待办」和「这个请求根本没问对」长得一模一样。
    """
    _seed_all_five(review_conn)

    response = _get(review_conn, "", tab="拼错了")

    assert response.status_code == 400
    # 报错要点名有哪几个合法值，不然用户只知道自己错了、不知道对的是什么。
    assert "fuzzy" in response.json()["detail"]


def test_counts_endpoint_reports_every_tab_including_the_empty_ones(review_conn):
    """空的那一页角标是 0，不是这个 key 不存在。

    缺 key 的话前端得写 `?? 0` 兜底，而那会把「后端没算这一页」和「这一页
    真的是 0」混成一件事。
    """
    _seed_reason(review_conn, "fuzzy_match_needs_confirmation")

    body = _get(review_conn, "/counts").json()

    assert sorted(body) == ["bad_type", "fuzzy", "out_of_ontology", "unresolved"]
    assert body["fuzzy"] == 1
    assert body["bad_type"] == 0


def test_counts_are_scoped_to_the_tenant(review_conn):
    """另一个租户的待办不能算进来。"""
    _seed_reason(review_conn, "fuzzy_match_needs_confirmation", tenant_id="t2")

    assert _get(review_conn, "/counts").json()["fuzzy"] == 0


def test_every_reason_the_pipeline_writes_lands_in_exactly_one_tab():
    """管线写入的每一种 reason 都必须落进恰好一个 tab。

    这条防的是最阴的那个 bug：normalization.py 将来加一种新 reason，没人
    记得更新映射，那批待办就永远不出现在任何页面上——队列里积着，界面上
    一片清净，而且没有任何报错。

    从源码里数出真实写入的 reason，而不是在这里手抄一份：手抄的那份跟
    normalization.py 一起漂移，这条用例就成了自说自话。
    """
    import pathlib
    import re

    from app.api.admin_graph_review_routes import TAB_REASONS

    covered = [r for reasons in TAB_REASONS.values() for r in reasons]
    assert sorted(covered) == sorted(set(covered)), "同一个 reason 落进了两个 tab"

    source = pathlib.Path("app/graphrag/normalization.py").read_text(encoding="utf-8")
    # 两种写法都要抓：关键字参数 reason="..." 和变量赋值 reason = "..."。
    # 只抓前者会漏掉 :202 那一行的 subject_unresolved / object_unresolved
    # ——而漏掉它们就意味着「一端对不上」那一页永远是空的。
    written = set(re.findall(r'reason\s*=\s*"([a-z_]+)"', source))
    # 那一行是三元表达式，两个分支的字面量在 if/else 两侧，上面的正则只吃
    # 得到第一个。第二个单独用它自己的形状抓。
    written |= set(re.findall(r'else\s+"([a-z_]+)"', source))

    assert written, "没从 normalization.py 里抓到任何 reason——正则失效了"
    assert written <= set(covered), f"这些 reason 没有归属的分页：{written - set(covered)}"


# ---------------------------------------------------------------------------
# 两个就地修复动作
#
# 「一端对不上」那一页要建一个实体，「不在本体」那一页要放宽本体。此前两件事
# 都得跳到别的页面做完再回来找那条待审——中间隔着一次导航和一次搜索，而
# 审核员手上正开着十几条。
# ---------------------------------------------------------------------------


async def _seed_draft_ontology(
    conn: aiosqlite.Connection, *, tenant_id: str, term_types: list[str],
    relation_type: str,
) -> None:
    """往草稿里插类型和关系类型。

    add_allowed_combination 的 _validate_references 校验的是 **draft** 的类型
    和关系类型（ontology_constraints.py:96-104），不是 confirmed——所以这里
    插 draft 行，跟 _seed_confirmed_ontology 是两件事。
    """
    for value in term_types:
        await conn.execute(
            "INSERT INTO ontology_term_types (tenant_id, value, status) VALUES (?, ?, 'draft')",
            (tenant_id, value),
        )
    await conn.execute(
        "INSERT INTO tenant_relation_types "
        "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
        "source, status) VALUES (?, ?, ?, '', 0, 'custom', 'draft')",
        (tenant_id, relation_type, relation_type),
    )
    await conn.commit()


def _post(review_conn, path: str, payload: dict, *, graph=None, tenant: str = "t1"):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph or FakeGraphClient()
    try:
        client = TestClient(app)
        return client.post(
            f"/api/admin/{tenant}/graph-reviews{path}", json=payload,
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()


def _enqueue_unresolved(review_conn) -> int:
    return asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="新面孔", object_candidate="认证模块",
            relation_type="RELATED_TO", reason="subject_unresolved", source="s.md",
            tenant_id="t1", subject_type_candidate="产品", object_type_candidate="模块",
        )
    )


def _seed_object_term(review_conn) -> None:
    asyncio.run(_seed_terms(review_conn, [
        Term(tenant_id="t1", node_key="模块:认证模块", standard_name="认证模块",
             aliases=[], term_type="模块", extra_properties={}),
    ]))


def test_create_missing_term_creates_it_and_approves_the_review(review_conn):
    """两件事一起做。

    分成两步的话，审核员建完实体还得回来手动批准，而中间任何中断都会留下一个
    「实体已建、审核还挂着」的状态——他下次看到这条会以为实体还没建。
    """
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="产品", object_term_type="模块",
        )
    )
    _seed_object_term(review_conn)
    review_id = _enqueue_unresolved(review_conn)

    response = _post(
        review_conn, f"/{review_id}/create-missing-term",
        {"standard_name": "新面孔", "term_type": "产品", "side": "subject"},
    )

    assert response.status_code == 200, response.text
    terms = asyncio.run(list_terms_merged(review_conn, "t1"))
    assert "新面孔" in [t.standard_name for t in terms]
    # 而且这条审核不再挂在待审里
    assert asyncio.run(count_pending_reviews(review_conn, tenant_id="t1")) == 0


def test_create_missing_term_leaves_the_review_pending_when_the_graph_write_fails(review_conn):
    """建实体成功但写图失败时，这条审核必须还在待审队列里。

    标成已批准的话，这条关系永远不会进图，而队列里也看不到它了——数据静悄悄
    地少了一条，没有任何地方能发现。
    """
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="产品", object_term_type="模块",
        )
    )
    _seed_object_term(review_conn)
    review_id = _enqueue_unresolved(review_conn)

    response = _post(
        review_conn, f"/{review_id}/create-missing-term",
        {"standard_name": "新面孔", "term_type": "产品", "side": "subject"},
        graph=UnavailableGraphClient(),
    )

    assert response.status_code == 503
    assert asyncio.run(count_pending_reviews(review_conn, tenant_id="t1")) == 1


def test_create_missing_term_refuses_a_type_not_in_the_ontology(review_conn):
    """新建实体的类型必须是本体里已有的。

    放行的话，审核这个动作自己就制造出了一个孤儿类型——而它绕过了本体那一层
    的全部校验，之后没有任何东西能匹配上它。
    """
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="产品", object_term_type="模块",
        )
    )
    review_id = _enqueue_unresolved(review_conn)

    response = _post(
        review_conn, f"/{review_id}/create-missing-term",
        {"standard_name": "新面孔", "term_type": "不存在的类型", "side": "subject"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    # 点名是哪个类型不够——去掉这道预检的话，create_term 自己也会抛
    # UnknownCategoryError（"未知分类: 'x'"），照样是 400 且含类型名，
    # 只断言这两点的用例分辨不出预检还在不在（变异验过）。
    assert "不存在的类型" in detail
    # 真正的差别在于**告诉他可以填什么**。"未知分类"只说了他错了，
    # 用户接下来仍然只能猜。
    assert "产品" in detail and "模块" in detail
    assert asyncio.run(count_pending_reviews(review_conn, tenant_id="t1")) == 1


def test_allow_combination_adds_it_to_the_draft_and_says_what_is_next(review_conn):
    """加进本体**草稿**，并说清下一步。

    加完不能顺手批准：add_allowed_combination 写的是 status='draft'
    （ontology_constraints.py:137），而 approve_review 查的是 confirmed——
    立刻批准必然撞 RelationNotInConfirmedOntologyError。
    """
    asyncio.run(
        _seed_draft_ontology(
            review_conn, tenant_id="t1", term_types=["产品", "模块"],
            relation_type="RELATED_TO",
        )
    )
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="某产品", object_candidate="认证模块",
            relation_type="RELATED_TO", reason="not_in_confirmed_ontology", source="s.md",
            tenant_id="t1", subject_type_candidate="产品", object_type_candidate="模块",
        )
    )

    response = _post(review_conn, f"/{review_id}/allow-combination", {})

    assert response.status_code == 200, response.text
    combos = asyncio.run(list_allowed_combinations(review_conn, "t1", status="draft"))
    assert ("产品", "RELATED_TO", "模块") in [
        (c.subject_term_type, c.relation_type, c.object_term_type) for c in combos
    ]
    # 回包要说清这条还没批准以及为什么。只回 {"ok": true} 的话，审核员会以为
    # 这条处理完了，而它还挂在队列里。
    assert "确认" in response.json()["next_step"]


def test_allow_combination_does_not_confirm_the_whole_draft(review_conn):
    """**不能顺手 confirm_ontology。**

    那个函数把整份草稿原地提升为已确认（ontology_lifecycle.py:249-253）。
    别人正在编辑中的半成品本体会被一次审核操作悄悄发布出去。

    草稿里另放一个跟这条审核无关的类型：确认整份草稿的实现会把它一起提升成
    confirmed，这条断言因此抓得住。只看被加的那个组合是分辨不出来的。
    """
    asyncio.run(
        _seed_draft_ontology(
            review_conn, tenant_id="t1", term_types=["产品", "模块", "别人正在编辑的类型"],
            relation_type="RELATED_TO",
        )
    )
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="某产品", object_candidate="认证模块",
            relation_type="RELATED_TO", reason="not_in_confirmed_ontology", source="s.md",
            tenant_id="t1", subject_type_candidate="产品", object_type_candidate="模块",
        )
    )

    assert _post(review_conn, f"/{review_id}/allow-combination", {}).status_code == 200

    confirmed = asyncio.run(list_term_types(review_conn, "t1", status="confirmed"))
    assert "别人正在编辑的类型" not in [c.value for c in confirmed]
    # 这条审核也还在待审里——加进草稿不等于处理完。
    assert asyncio.run(count_pending_reviews(review_conn, tenant_id="t1")) == 1


def test_allow_combination_refuses_when_the_types_are_unknown(review_conn):
    """管线没识别出类型时不能加白名单。

    加进去的会是一个带空类型的组合，它匹配不上任何东西——白名单里多了一条
    永远不生效的规则，而用户以为自己已经放宽了本体。
    """
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="某产品", object_candidate="认证模块",
            relation_type="RELATED_TO", reason="not_in_confirmed_ontology", source="s.md",
            tenant_id="t1",
        )
    )

    response = _post(review_conn, f"/{review_id}/allow-combination", {})

    assert response.status_code == 400
    assert "类型" in response.json()["detail"]


def test_create_missing_term_says_so_when_the_name_is_held_by_a_deleted_entity(review_conn):
    """名字被一条**已人工删除**的实体占着时，说清楚并给出路。

    `_check_name_conflict` 查 terms 裸表（看得见那条），而 `_approve_with_names`
    查合并视图（看不见）。吞掉冲突直接往下走的话，用户会连着收到两句自相
    矛盾的话——"已经有了"然后"不在术语表里"——而且从这个界面无论如何都
    走不出去。
    """
    asyncio.run(
        _seed_confirmed_ontology(
            review_conn, tenant_id="t1", relation_type="RELATED_TO",
            subject_term_type="产品", object_term_type="模块",
        )
    )
    asyncio.run(_seed_terms(review_conn, [
        Term(tenant_id="t1", node_key="产品:新面孔", standard_name="新面孔",
             aliases=[], term_type="产品", extra_properties={}),
        Term(tenant_id="t1", node_key="模块:认证模块", standard_name="认证模块",
             aliases=[], term_type="模块", extra_properties={}),
    ]))

    async def soft_delete():
        await upsert_term_edit(
            review_conn, tenant_id="t1", node_key="产品:新面孔",
            field=FIELD_DELETED, value="1", edited_by="alice",
        )

    asyncio.run(soft_delete())
    review_id = _enqueue_unresolved(review_conn)

    response = _post(
        review_conn, f"/{review_id}/create-missing-term",
        {"standard_name": "新面孔", "term_type": "产品", "side": "subject"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "已人工删除" in detail
    # 两条路都要给：只说"被占着"的话用户仍然不知道该干什么。
    assert "恢复" in detail and "换一个名字" in detail
    assert asyncio.run(count_pending_reviews(review_conn, tenant_id="t1")) == 1
