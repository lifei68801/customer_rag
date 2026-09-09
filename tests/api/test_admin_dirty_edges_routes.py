"""整个租户的脏边清单。

脏边的列举此前只在实体详情页里——你得**先知道是哪个实体**才看得到它的脏边，
而脏边的特点恰恰是没人知道它们在哪。这一页解决的就是那个"不知道去哪找"。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app
from tests.schema_fixtures import ensure_admin_auth_schema
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


def _edge(i: int) -> dict:
    return {
        "subject_node_key": f"产品:P{i}",
        "subject_standard_name": f"P{i}",
        "relation_type": "RELATED_TO",
        "object_node_key": "模块:M",
        "object_standard_name": "M",
        "edge_tenant_id": None,
        "subject_tenant_id": "t1",
        "object_tenant_id": "t1",
    }


class FakeGraph:
    def __init__(self, *, count: int = 2, truncated: bool = False) -> None:
        self._edges = [_edge(i) for i in range(count)]
        self._truncated = truncated
        self.calls: list[dict] = []

    async def list_tenant_dirty_edges(self, *, tenant_id: str, limit: int = 500):
        self.calls.append({"tenant_id": tenant_id, "limit": limit})
        return self._edges, self._truncated


class BrokenGraph:
    async def list_tenant_dirty_edges(self, *, tenant_id: str, limit: int = 500):
        raise ConnectionError("Neo4j 不可用")


async def _open_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    try:
        await create_tenants_table(conn)
        await ensure_admin_auth_schema(conn)
        await create_admin_user(
            conn, username="alice", password="password1", role="admin", tenant_id=None
        )
        await create_tenant(conn, tenant_id="t1", name="t1")
    except BaseException:
        await conn.close()
        raise
    return conn


@pytest.fixture()
def conn():
    c = asyncio.run(_open_conn())
    try:
        yield c
    finally:
        asyncio.run(c.close())


def _get(conn, *, graph=None, params: dict | None = None):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph or FakeGraph()
    try:
        token = session_store.create_session(username="alice", role="admin", tenant_id=None)
        client = TestClient(app)
        return client.get(
            "/api/admin/t1/dirty-edges", params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.clear()


def test_lists_the_whole_tenants_dirty_edges(conn):
    """不锚在某个实体上——这就是这一页存在的理由。"""
    response = _get(conn)

    assert response.status_code == 200
    body = response.json()
    assert len(body["edges"]) == 2
    # 每条都要说清是谁 -什么关系-> 谁，以及是哪一侧的租户标记不对。少任何
    # 一样，运维都没法判断该不该删。
    edge = body["edges"][0]
    assert edge["subject_node_key"] == "产品:P0"
    assert edge["relation_type"] == "RELATED_TO"
    assert edge["object_node_key"] == "模块:M"
    assert "edge_tenant_id" in edge


def test_says_when_the_listing_was_truncated(conn):
    """超过上限时必须说出来。

    默默少列的话，运维会以为脏边只有 500 条——他清完那 500 条就以为干净了。
    """
    body = _get(conn, graph=FakeGraph(count=500, truncated=True)).json()

    assert body["truncated"] is True
    assert body["limit"] == 500


def test_does_not_claim_truncation_when_everything_fits(conn):
    """没超时不能说被截断了。

    报成截断的话运维会去找一批根本不存在的脏边。
    """
    assert _get(conn).json()["truncated"] is False


def test_a_graph_failure_is_an_error_not_an_empty_list(conn):
    """图谱查不通时返回 5xx，不是空列表。

    空列表读起来是「一条脏边都没有」——那是这一页最不该说错的一句话，
    运维会据此认为数据是干净的。
    """
    response = _get(conn, graph=BrokenGraph())

    assert response.status_code == 503
    assert "不代表没有脏边" in response.json()["detail"]


def test_limit_is_passed_through(conn):
    """调用方能收窄上限。"""
    graph = FakeGraph()

    _get(conn, graph=graph, params={"limit": 10})

    assert graph.calls[0]["limit"] == 10
