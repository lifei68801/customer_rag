"""非 Neo4j 后端下，后台管理路径要在一处失败，而不是十五处。

图谱这边只有一个真接缝（`GraphReadProtocol`，两个适配器都实现）。除此之外
的十几个方法只有 `Neo4jGraphClient` 有，`NeptuneGraphClient` 上是显式存根。
在 `deps.get_neo4j_graph_client` 出现之前，拿着 neptune 后端点开后台任意
页面，得到的是十五种 NotImplementedError 之一，深在某个请求中途，消息里
还指着一个不存在的文档路径。

这不是新的限制，是把既有的限制说出来。
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


class FakeGraph:
    """什么都不做——这些请求根本走不到图客户端那一步。"""

    async def list_tenant_dirty_edges(self, **kwargs):  # pragma: no cover
        raise AssertionError("闸没拦住，请求走到图客户端了")


async def _open_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
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


def _get(conn, *, graph_backend: str):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(
        admin_token="tok", graph_backend=graph_backend
    )
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraph()
    try:
        token = session_store.create_session(
            username="alice", role="admin", tenant_id=None
        )
        return TestClient(app).get(
            "/api/admin/t1/dirty-edges",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.clear()


def test_neptune_backend_is_refused_with_an_explanation(conn):
    """501，并且说清缺的是什么、该配成什么。

    501 而不是 500：这不是出错，是这个后端没实现这部分功能。
    """
    response = _get(conn, graph_backend="neptune")

    assert response.status_code == 501
    detail = response.json()["detail"]
    assert "neptune" in detail
    # 要说清"少了什么"和"该怎么办"，否则运维只知道点不动、不知道为什么。
    assert "neo4j" in detail


def test_neo4j_backend_passes_through(conn):
    """默认后端照常放行——这条闸不能把正常路径也关上。

    没有这一条的话，"永远返回 501"这种实现也能让上面那条通过。
    """
    response = _get(conn, graph_backend="neo4j")

    assert response.status_code != 501
