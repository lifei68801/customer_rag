"""实体邻域图端点。

这一页最容易退化成静默失败的地方是**截断**：默默少画的话，用户对着一张画了
300 个邻居的图下「这个实体只连了 300 个东西」的结论——而它连着 1013 个。
"""
from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from tests.schema_fixtures import ensure_admin_auth_schema
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


class FakeGraph:
    """按邻居数量造一张星形图：中心连着 N 个邻居，每人一条边。"""

    def __init__(self, *, neighbours: int = 2, broken: bool = False) -> None:
        self._neighbours = neighbours
        self._broken = broken
        self.calls: list[dict] = []

    async def query_neighborhood(
        self, node_key: str, *, tenant_id: str, chain_query_relation_types: set[str]
    ):
        self.calls.append(
            {"node_key": node_key, "chain_query_relation_types": chain_query_relation_types}
        )
        if self._broken:
            raise ConnectionError("Neo4j 不可用")
        return [
            {
                "source_node_key": "产品:Beer", "source_name": "Beer", "source_type": "产品",
                "relation_type": "HAS_SKU",
                "target_node_key": f"SKU:{i}", "target_name": f"SKU{i}", "target_type": "SKU",
            }
            for i in range(self._neighbours)
        ]


async def _open_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    try:
        await ensure_terms_schema(conn)
        await ensure_term_edits_schema(conn)
        await ensure_ontology_schema(conn)
        await create_tenants_table(conn)
        await ensure_admin_auth_schema(conn)
        await create_admin_user(
            conn, username="alice", password="password1", role="admin", tenant_id=None
        )
        await create_tenant(conn, tenant_id="t1", name="t1")
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES ('t1', '产品:Beer', 'Beer', '[]', '产品', ?, 'etl')",
            (json.dumps({}),),
        )
        # 两个已确认的关系类型，只有一个勾了「支持链式查询」——写死一组的
        # 实现会把两个都传进去，那条用例因此抓得住。
        for relation_type, chain in (("HAS_SKU", 1), ("RELATED_TO", 0)):
            await conn.execute(
                "INSERT INTO tenant_relation_types "
                "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
                "source, status) VALUES ('t1', ?, ?, '', ?, 'custom', 'confirmed')",
                (relation_type, relation_type, chain),
            )
        await conn.commit()
    except BaseException:
        await conn.close()
        raise
    return conn


@pytest.fixture()
def preview_conn():
    conn = asyncio.run(_open_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _preview(
    conn, *, node_key: str = "产品:Beer", graph=None, tenant: str = "t1",
    username: str = "alice", role: str = "admin",
):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph or FakeGraph()
    try:
        token = session_store.create_session(username=username, role=role, tenant_id=None)
        client = TestClient(app)
        return client.get(
            f"/api/admin/{tenant}/graph-preview/{node_key}",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.clear()


def test_returns_the_center_and_its_neighbours(preview_conn):
    """中心节点必须在 nodes 里。

    不在的话前端得自己补一个，而它补出来的那个没有 term_type——图上会出现
    一个没有颜色的孤点，而那恰恰是用户点开这一页要看的那个实体。
    """
    body = _preview(preview_conn).json()

    assert body["center"] == "产品:Beer"
    keys = {n["node_key"] for n in body["nodes"]}
    assert "产品:Beer" in keys
    assert keys == {"产品:Beer", "SKU:0", "SKU:1"}
    center = next(n for n in body["nodes"] if n["node_key"] == "产品:Beer")
    assert center["term_type"] == "产品"


def test_an_entity_with_no_edges_still_returns_its_own_node(preview_conn):
    """一条边都没有的实体也要有它自己那个点。

    返回空 nodes 的话，前端画出来的是一片空白——用户分不清"这个实体没有
    关系"和"这一页坏了"。
    """
    body = _preview(preview_conn, graph=FakeGraph(neighbours=0)).json()

    assert [n["node_key"] for n in body["nodes"]] == ["产品:Beer"]
    assert body["edges"] == []
    assert body["truncated"] is False


def test_truncates_at_the_cap_and_says_so(preview_conn):
    """超过上限时 truncated=true，且 total_nodes 是**真实总数**不是上限。

    这是整个功能里最容易退化成静默失败的地方：默默少画的话，用户对着一张
    画了 300 个邻居的图下「这个实体只连了 300 个东西」的结论——而它连着
    1013 个。
    """
    body = _preview(preview_conn, graph=FakeGraph(neighbours=1013)).json()

    assert body["truncated"] is True
    assert body["total_nodes"] == 1014  # 1013 个邻居 + 中心
    assert body["shown_nodes"] == 300
    assert len(body["nodes"]) == 300


def test_the_center_survives_truncation(preview_conn):
    """截断时中心节点一定留下。

    把中心切掉的话，用户看到的是一张不含他要看的那个实体的图——而那正是
    他点开这一页的全部理由。
    """
    body = _preview(preview_conn, graph=FakeGraph(neighbours=1013)).json()

    assert "产品:Beer" in {n["node_key"] for n in body["nodes"]}


def test_does_not_claim_truncation_when_everything_fits(preview_conn):
    """没截断时 truncated=false。

    恒为 true 的实现会让每张图都挂着一句吓人的提示，用户很快就不看它了。
    """
    body = _preview(preview_conn).json()

    assert body["truncated"] is False
    assert body["total_nodes"] == body["shown_nodes"] == 3


def test_edges_reference_only_nodes_that_are_in_the_payload(preview_conn):
    """截断之后，指向被截掉节点的边也要一起去掉。

    留着的话 sigma 找不到端点会抛异常，整张图变白——比少画几个节点糟得多。
    这条在「先截节点、忘了截边」的实现下必红。
    """
    body = _preview(preview_conn, graph=FakeGraph(neighbours=1013)).json()

    keys = {n["node_key"] for n in body["nodes"]}
    assert body["edges"], "截断之后仍应有边留下，否则这条用例什么都没验到"
    for edge in body["edges"]:
        assert edge["source"] in keys
        assert edge["target"] in keys


def test_a_nonexistent_node_key_is_a_404(preview_conn):
    """不存在的实体返回 404，不是一张空图。

    空图看起来像「这个实体一条关系都没有」——而真相是这个实体根本不存在，
    两句话要用户做的事完全不同。
    """
    response = _preview(preview_conn, node_key="产品:根本没有这个")

    assert response.status_code == 404
    assert "产品:根本没有这个" in response.json()["detail"]


def test_chain_relation_types_come_from_the_tenant_config(preview_conn):
    """两跳走哪些关系必须来自 tenant_relation_types，不是写死的一组。

    写死的话，预览里能走两跳的关系和问答里能走两跳的关系不一样——而用户会
    拿这两处互相印证：他在预览里看到 A 两跳能到 C，回去问却答不出来。

    租户里有两个已确认关系类型，只有 HAS_SKU 勾了链式：传两个的实现会红。
    """
    graph = FakeGraph()

    _preview(preview_conn, graph=graph)

    assert graph.calls[0]["chain_query_relation_types"] == {"HAS_SKU"}


def test_a_graph_failure_is_an_error_not_an_empty_graph(preview_conn):
    """图谱查不通时返回 5xx，不是空图。

    空图读起来是「这个实体没有关系」——而那是一句可能不实的断言。
    """
    response = _preview(preview_conn, graph=FakeGraph(broken=True))

    assert response.status_code == 503
    assert "不代表这个实体没有关系" in response.json()["detail"]
