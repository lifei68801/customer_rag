"""看板的两个端点。

**为什么是两个而不是一个**：spec D5 的裁决二要求「每个领域一张卡、各自
独立请求、各自落位」。一个端点返回全部统计的话，前端只能等最慢的那个领域
算完才画得出第一张卡。

两个端点的作用域也不同，这是本文件最要紧的一条：清单是**非租户**路径
（「我能看到哪些领域」对每个角色都要回答），逐领域统计是**租户内**路径。
清单端点过滤了、统计端点不校验的话，member 直接改 URL 就能拿到别人的
统计数字——聚合数字也是信息，它泄露的是业务规模。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user
from app.auth.user_tenants_store import grant_tenant_access
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.organizations_store import (
    assign_tenant_to_org,
    create_organization,
    ensure_organizations_schema,
)
from app.graphrag.duplicate_review_queue import ensure_duplicate_review_schema
from app.graphrag.review_queue import ensure_review_schema, enqueue_for_review
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.tenant_personas_store import ensure_tenant_personas_schema
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import create_term, ensure_terms_schema
from app.ingestion.tracking import ensure_tracking_schema, record_ingested
from app.main import app
from tests.schema_fixtures import ensure_admin_auth_schema, ensure_review_queues_schema
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


class FakeGraph:
    """按租户报告边数；`broken` 里的租户抛异常（图谱连不上）。"""

    def __init__(self, edges: dict[str, int] | None = None, *, broken: set[str] | None = None):
        self._edges = edges or {}
        self._broken = broken or set()

    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int:
        if tenant_id in self._broken:
            raise RuntimeError("Neo4j 连不上")
        return self._edges.get(tenant_id, 0)


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_admin_auth_schema(conn)
    await create_tenants_table(conn)
    await ensure_organizations_schema(conn)
    await ensure_tenant_personas_schema(conn)
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_review_queues_schema(conn)

    await create_admin_user(
        conn, username="root", password="password1", role="admin", tenant_id=None
    )
    # admin_users 的 CHECK 要求 member 有非空 tenant_id（存量回退值）。
    # alice 在 user_tenants 里有显式授权，这一列取值不影响本文件的判定。
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="muji-goods"
    )

    # 三个租户：alice 被授权 muji-goods 与 muji-store，secret 只有 admin 看得到。
    for tenant_id, name in (
        ("muji-goods", "商品"),
        ("muji-store", "门店"),
        ("secret", "机密"),
    ):
        await create_tenant(conn, tenant_id=tenant_id, name=name)
        await checkout_draft(conn, tenant_id)
        await create_term_type(conn, tenant_id=tenant_id, value="产品", actor="alice")
        await confirm_ontology(conn, tenant_id, actor="alice")
    await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
    await grant_tenant_access(conn, username="alice", tenant_id="muji-store")

    # muji-goods 挂在组织下，muji-store 不挂——没挂组织是合法状态（存量租户）。
    await create_organization(conn, org_id="muji", name="无印良品")
    await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")

    # 四个互不相同的数字：13 实体 / 27 边 / 5 文档 / 3 待办。相同的话，
    # 把实体数接到关系数那一格的实现也能变绿。
    for i in range(13):
        await create_term(
            conn, tenant_id="muji-goods", standard_name=f"P{i}", aliases=[], term_type="产品"
        )
    for i in range(3):
        await enqueue_for_review(
            conn, subject_candidate=f"S{i}", object_candidate=f"O{i}",
            relation_type="RELATED_TO", reason="测试", source="t.md", tenant_id="muji-goods",
        )
    return conn


async def _open_ingestion_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    for i in range(5):
        await record_ingested(
            conn, tenant_id="muji-goods", file_path=f"d{i}.md", content_hash=f"h{i}", chunk_count=1
        )
    return conn


@pytest.fixture()
def dashboard_conns():
    review_conn = asyncio.run(_open_review_conn())
    ingestion_conn = asyncio.run(_open_ingestion_conn())
    try:
        yield review_conn, ingestion_conn
    finally:
        asyncio.run(review_conn.close())
        asyncio.run(ingestion_conn.close())


def _client(conns, *, username: str, role: str, graph=None):
    review_conn, ingestion_conn = conns
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    # 必须显式覆盖：不覆盖的话 deps.get_graph_client 会去连真实 Neo4j。
    app.dependency_overrides[deps.get_graph_client] = lambda: graph or FakeGraph({"muji-goods": 27})
    token = session_store.create_session(username=username, role=role, tenant_id=None)
    # raise_server_exceptions=False：图谱故障那条用例要看到 HTTP 状态码，
    # 而不是让异常穿过 TestClient 直接抛进用例里。
    return TestClient(app, raise_server_exceptions=False), {"Authorization": f"Bearer {token}"}


def _get_domains(conns, *, username: str = "alice", role: str = "member"):
    try:
        client, headers = _client(conns, username=username, role=role)
        return client.get("/api/admin/dashboard/domains", headers=headers)
    finally:
        app.dependency_overrides.clear()


def _get_stats(
    conns, *, tenant_id: str = "muji-goods", username: str = "alice",
    role: str = "member", graph=None,
):
    try:
        client, headers = _client(conns, username=username, role=role, graph=graph)
        return client.get(f"/api/admin/{tenant_id}/dashboard/stats", headers=headers)
    finally:
        app.dependency_overrides.clear()


def test_domains_endpoint_lists_only_what_this_account_can_reach(dashboard_conns):
    """看板只显示有权访问的领域。

    聚合数字也是信息——给 member 看到他无权访问的领域有多少实体，等于
    泄露业务规模（Global Constraint 13）。
    """
    body = _get_domains(dashboard_conns).json()
    assert [d["tenant_id"] for d in body["domains"]] == ["muji-goods", "muji-store"]


def test_admin_sees_every_domain(dashboard_conns):
    """admin 看得到全部——否则上面那条用例在「谁都看不到」的实现下也能绿。"""
    body = _get_domains(dashboard_conns, username="root", role="admin").json()
    assert [d["tenant_id"] for d in body["domains"]] == ["muji-goods", "muji-store", "secret"]


def test_domains_endpoint_returns_no_numbers(dashboard_conns):
    """清单端点不带统计。

    带上的话它就得等所有领域都算完才返回，「每张卡各自落位」这个设计就没了
    ——第一张卡也要等最慢的那个领域。
    """
    first = _get_domains(dashboard_conns).json()["domains"][0]
    assert set(first) == {"tenant_id", "name", "org_id", "org_name"}


def test_domains_carry_their_organization(dashboard_conns):
    """卡片要按组织分组，所以清单里得有 org_id 和 org_name。

    没挂组织的租户 org_id 为 null——那是合法状态（存量租户），不是错误。
    """
    domains = {d["tenant_id"]: d for d in _get_domains(dashboard_conns).json()["domains"]}
    assert (domains["muji-goods"]["org_id"], domains["muji-goods"]["org_name"]) == ("muji", "无印良品")
    assert (domains["muji-store"]["org_id"], domains["muji-store"]["org_name"]) == (None, None)


def test_stats_endpoint_returns_all_four_numbers(dashboard_conns):
    """四个数字互不相同，逐个断言。相同的话接错线也能绿。"""
    body = _get_stats(dashboard_conns).json()
    assert body == {
        "tenant_id": "muji-goods",
        "term_count": 13,
        "edge_count": 27,
        "document_count": 5,
        "pending_review_count": 3,
        "sheet_row_count": 0,
        "stale_question_count": 0,
        "review_alias_count": 0,
        "review_alias_hits": 0,
        # 5 篇文档、3 条待审，都是刚写的，落在近 30 天窗口里。
        "recent_reviews_per_document": 0.6,
    }


def test_stats_endpoint_refuses_a_tenant_this_account_cannot_reach(dashboard_conns):
    """逐领域端点必须走 require_tenant_access。

    清单端点过滤了、这个不校验的话，member 直接改 URL 就能拿到别人的统计。
    """
    assert _get_stats(dashboard_conns, tenant_id="secret").status_code == 403


def test_a_graph_failure_returns_an_error_not_zeroes(dashboard_conns):
    """图谱挂了时这张卡返回 5xx，前端据此渲染「统计失败」。

    返回全 0 的话用户会以为这个领域是空的——一个看起来正常、实际是错的
    数字，他会去重跑一遍 ETL 找那些"丢了"的数据。
    """
    resp = _get_stats(dashboard_conns, graph=FakeGraph(broken={"muji-goods"}))
    # 503 而不是笼统的 >= 500：把整个 try/except 删掉的话异常会穿到
    # FastAPI 的兜底处理，那也是 5xx，但回给用户的是一句
    # "Internal Server Error"——它不说这不代表领域是空的，也不说可以重试。
    assert resp.status_code == 503
    # 数字一个都不能出现在回包里：给了 0 就等于回答了「有多少」。
    assert "term_count" not in resp.text
    # 那句话本身要在。前端把它原样显示在卡片上，它是用户唯一能读到的解释。
    assert "这不代表这个领域是空的" in resp.json()["detail"]


def test_anonymous_requests_are_refused(dashboard_conns):
    """不带会话的请求得 401。「我能看到哪些领域」对匿名者没有答案。"""
    try:
        client, _ = _client(dashboard_conns, username="alice", role="member")
        assert client.get("/api/admin/dashboard/domains").status_code == 401
    finally:
        app.dependency_overrides.clear()


# ── 图谱结构 ────────────────────────────────────────────────────────────


class FakeStructureGraph(FakeGraph):
    """按关系类型的边数、连通实体数、扇出探测都能打桩的图谱。"""

    def __init__(
        self,
        *,
        edges_by_type: dict[str, int] | None = None,
        connected: tuple[int, int] = (0, 0),
        fanout: dict[tuple[str, str, str], int | None] | None = None,
        fanout_raises: bool = False,
        broken: set[str] | None = None,
    ):
        super().__init__({}, broken=broken)
        self._edges_by_type = edges_by_type or {}
        self._connected = connected
        self._fanout = fanout or {}
        self._fanout_raises = fanout_raises

    async def count_edges_by_relation_type(self, *, tenant_id: str) -> dict[str, int]:
        if tenant_id in self._broken:
            raise RuntimeError("Neo4j 连不上")
        return dict(self._edges_by_type)

    async def count_connected_terms(self, *, tenant_id: str) -> tuple[int, int]:
        return self._connected

    async def probe_relation_fanout(
        self, *, tenant_id: str, relation_type: str, from_term_type: str,
        to_term_type: str, direction: str,
    ) -> int | None:
        if self._fanout_raises:
            raise RuntimeError("探测失败")
        return self._fanout.get((from_term_type, relation_type, to_term_type))


def _get_structure(conns, *, tenant_id: str = "muji-goods", graph=None):
    try:
        client, headers = _client(conns, username="alice", role="member", graph=graph)
        return client.get(f"/api/admin/{tenant_id}/dashboard/structure", headers=headers)
    finally:
        app.dependency_overrides.clear()


def _seed_confirmed_constraint(conns, *, tenant_id: str = "muji-goods") -> None:
    """给这个租户加一条已确认的约束：产品 -RELATED_TO-> 产品。

    没有约束就没有扇出探测可做，risks 恒为空——那样的话，"扇出 1 不算风险"
    "探测失败不算风险"这些用例不管实现怎么写都是绿的（第一版正是这样，被变异
    测试抓了出来）。
    """
    review_conn, _ = conns

    async def _run() -> None:
        from app.graphrag.ontology_constraints import add_allowed_combination

        await checkout_draft(review_conn, tenant_id)
        # RELATED_TO 是平台预置的关系类型，草稿里本来就有，不用（也不能）再建。
        await add_allowed_combination(
            review_conn, tenant_id, subject_term_type="产品",
            relation_type="RELATED_TO", object_term_type="产品", actor="alice",
        )
        await confirm_ontology(review_conn, tenant_id, actor="alice")

    asyncio.run(_run())


def test_structure_returns_composition_sorted_by_size(dashboard_conns):
    """构成条要先画最大的那一类。乱序的话用户读不出"图谱主要由什么撑起来"。"""
    response = _get_structure(
        dashboard_conns,
        graph=FakeStructureGraph(
            edges_by_type={"HAS_COMPANY": 30, "HAS_PRODUCT": 10000},
            connected=(13, 10),
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["entity_types"] == [{"term_type": "产品", "count": 13}]
    assert [r["relation_type"] for r in body["relation_types"]] == ["HAS_PRODUCT", "HAS_COMPANY"]
    assert (body["graph_term_count"], body["connected_term_count"]) == (13, 10)


def test_structure_reports_only_edges_whose_fanout_really_exceeds_one(dashboard_conns):
    """扇出 1 是函数关系，不是风险；未知（探测失败）也不算风险——把"查不到"
    显示成"有风险"是另一种撒谎。"""
    _seed_confirmed_constraint(dashboard_conns)

    response = _get_structure(
        dashboard_conns,
        graph=FakeStructureGraph(fanout={("产品", "RELATED_TO", "产品"): 1}),
    )

    assert response.json()["risks"] == []


def test_a_failed_fanout_probe_does_not_fail_the_whole_structure(dashboard_conns):
    """一条边探不出来，其余结构信息照常给。整块报错的话，用户连"图里有什么"
    都看不到了。"""
    _seed_confirmed_constraint(dashboard_conns)

    response = _get_structure(
        dashboard_conns,
        graph=FakeStructureGraph(edges_by_type={"HAS_PRODUCT": 7}, fanout_raises=True),
    )

    assert response.status_code == 200
    assert response.json()["risks"] == []
    assert response.json()["relation_types"] == [{"relation_type": "HAS_PRODUCT", "edge_count": 7}]


def test_structure_reports_a_real_fan_trap(dashboard_conns):
    """扇出 > 1 必须报出来，否则这块只是个永远为空的装饰。

    这是 demo 上真实发生过的那类问题：产品→公司 扇出为 3，沿它做计数聚合
    会让"某公司有多少订单"恒等于订单总数。本体层完全正常，只有真实数据
    看得出来。
    """
    _seed_confirmed_constraint(dashboard_conns)

    response = _get_structure(
        dashboard_conns,
        graph=FakeStructureGraph(fanout={("产品", "RELATED_TO", "产品"): 3}),
    )

    assert response.json()["risks"] == [
        {
            "subject_term_type": "产品",
            "relation_type": "RELATED_TO",
            "object_term_type": "产品",
            "fanout": 3,
        }
    ]


def test_a_graph_failure_returns_503_not_an_empty_structure(dashboard_conns):
    """空列表在界面上长得像"这个图没有关系"——一个看起来正常、实际是错的结论。"""
    response = _get_structure(
        dashboard_conns, graph=FakeStructureGraph(broken={"muji-goods"}),
    )

    assert response.status_code == 503
    assert "不代表这个图是空的" in response.json()["detail"]


def test_structure_refuses_a_tenant_this_account_cannot_reach(dashboard_conns):
    """结构本身也是业务信息：有哪几类实体、多少条边，同样不能给无权的人看。"""
    response = _get_structure(dashboard_conns, tenant_id="secret")

    assert response.status_code in (403, 404)
