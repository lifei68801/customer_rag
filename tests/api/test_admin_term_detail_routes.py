from __future__ import annotations

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
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from tests.settings_factory import build_settings

# 实体详情页需要「这个实体在图谱里连着什么」——列表行里放不下，但它正是
# GraphRAG 的核心：一个实体有没有用，取决于它连着谁。
#
# 已有的 query_subgraph 不够用：它只返回邻居的 standard_name，详情页要能
# 点击跳到邻居，需要 node_key。


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await create_tenants_table(conn)
    # require_admin_session 现在每个请求都要确认账号仍是 active，
    # 所以本体库里必须有这张表和一个可用的管理员。
    await ensure_admin_users_schema(conn)
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
    await create_tenant(conn, tenant_id="demo", name="demo")
    return conn


@pytest.fixture
def review_conn():
    """必须显式 close：aiosqlite 的后台线程不是 daemon，泄漏连接会让 pytest
    跑完全部用例后卡在解释器退出阶段。"""
    conn = asyncio.run(_open_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def _seed(conn: aiosqlite.Connection, terms: list[Term]) -> None:
    for t in terms:
        await conn.execute(
            "INSERT OR REPLACE INTO terms (tenant_id, node_key, standard_name, aliases,"
            " term_type, extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, 'etl')",
            (
                t.tenant_id, t.node_key, t.standard_name,
                json.dumps(t.aliases, ensure_ascii=False), t.term_type,
                json.dumps(t.extra_properties, ensure_ascii=False),
            ),
        )
    await conn.commit()


class FakeGraphClient:
    """只实现详情页要用的那一个方法。"""

    def __init__(self, relations=None, fail=False):
        self._relations = relations or []
        self._fail = fail
        self.asked: list[tuple[str, str]] = []

    async def list_term_relations(self, *, tenant_id: str, node_key: str):
        if self._fail:
            raise ConnectionError("Neo4j 不可用")
        self.asked.append((tenant_id, node_key))
        return self._relations


def _get(review_conn, graph_client, node_key: str, tenant_id: str = "demo"):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        token = session_store.create_session(username="admin", role="admin", tenant_id=None)
        return client.get(
            f"/api/admin/{tenant_id}/terms/{node_key}",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.clear()


def test_returns_the_term_with_its_relations(review_conn):
    asyncio.run(_seed(review_conn, [
        Term(tenant_id="demo", node_key="公司:可口可乐", standard_name="可口可乐",
             aliases=["Coca-Cola"], term_type="公司", extra_properties={"sku": "A1"}),
    ]))
    graph = FakeGraphClient(relations=[
        {"direction": "out", "relation_type": "生产",
         "node_key": "产品:雪碧", "standard_name": "雪碧", "term_type": "产品"},
    ])

    response = _get(review_conn, graph, "公司:可口可乐")

    assert response.status_code == 200
    body = response.json()
    assert body["standard_name"] == "可口可乐"
    assert body["aliases"] == ["Coca-Cola"]
    assert body["extra_properties"] == {"sku": "A1"}
    # 邻居必须带 node_key：详情页要能点过去，光有显示名跳不了。
    assert body["relations"] == [
        {"direction": "out", "relation_type": "生产",
         "node_key": "产品:雪碧", "standard_name": "雪碧", "term_type": "产品"},
    ]


def test_no_relations_is_not_an_error(review_conn):
    """孤立实体是一个真实且重要的状态——它对检索基本无用。返回空数组让
    前端能明确说出这件事，而不是当成加载失败。"""
    asyncio.run(_seed(review_conn, [
        Term(tenant_id="demo", node_key="公司:孤儿", standard_name="孤儿",
             aliases=[], term_type="公司"),
    ]))

    body = _get(review_conn, FakeGraphClient(relations=[]), "公司:孤儿").json()

    assert body["relations"] == []


def test_graph_down_still_returns_the_term(review_conn):
    """Neo4j 挂了不该让整个详情页打不开——实体的属性存在 SQLite 里，
    照样能看能改。关系那块单独标注拉取失败即可。"""
    asyncio.run(_seed(review_conn, [
        Term(tenant_id="demo", node_key="公司:可口可乐", standard_name="可口可乐",
             aliases=[], term_type="公司"),
    ]))

    response = _get(review_conn, FakeGraphClient(fail=True), "公司:可口可乐")

    assert response.status_code == 200
    body = response.json()
    assert body["standard_name"] == "可口可乐"
    assert body["relations"] is None, "拉取失败要跟「确实没有关系」区分开"


def test_unknown_node_key_is_404(review_conn):
    response = _get(review_conn, FakeGraphClient(), "公司:不存在")

    assert response.status_code == 404


def test_node_key_with_special_chars_survives_the_url(review_conn):
    """node_key 形如「公司:可口可乐」，冒号和中文都要能过 URL。这类实体
    是 ETL 主力产出，路径参数处理不当会让详情页对它们全部打不开。"""
    asyncio.run(_seed(review_conn, [
        Term(tenant_id="demo", node_key="订单号:0-00-008362-3",
             standard_name="0-00-008362-3", aliases=[], term_type="订单号"),
    ]))
    graph = FakeGraphClient(relations=[])

    from urllib.parse import quote

    response = _get(review_conn, graph, quote("订单号:0-00-008362-3", safe=""))

    assert response.status_code == 200
    assert graph.asked == [("demo", "订单号:0-00-008362-3")]
