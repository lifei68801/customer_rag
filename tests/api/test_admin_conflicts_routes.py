"""属性冲突的列表与决议端点。

决议不是"改一下冲突表的状态"这么简单：选中的值要真的进 terms、还要同步进
图谱。少做任何一步，界面上都会显示"已处理"而数据是旧的——那正是这整个
计划要消灭的那类静默失败，只不过换了个位置。
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
from app.graphrag.attribute_conflicts import (
    count_conflicts,
    ensure_attribute_conflicts_schema,
    list_conflicts,
    record_conflict,
)
from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from tests.schema_fixtures import ensure_admin_auth_schema
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


class FakeGraph:
    def __init__(self) -> None:
        self.synced: list = []

    async def sync_term(self, term) -> None:
        self.synced.append(term)


class BrokenGraph:
    async def sync_term(self, term) -> None:
        raise ConnectionError("Neo4j 不可用")


async def _open_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    try:
        await ensure_terms_schema(conn)
        await ensure_term_edits_schema(conn)
        await ensure_attribute_conflicts_schema(conn)
        await create_tenants_table(conn)
        await ensure_admin_auth_schema(conn)
        await create_admin_user(
            conn, username="alice", password="password1", role="admin", tenant_id=None
        )
        await create_tenant(conn, tenant_id="t1", name="t1")
        # 直接插 terms，绕开 create_term 的分类校验——这批用例只关心决议
        # 把值写没写回去，跟本体校验无关。
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, 'etl')",
            ("t1", "产品:洗发水", "洗发水", "[]", "产品",
             json.dumps({"price": "39", "origin": "日本"}, ensure_ascii=False)),
        )
        await conn.commit()
        await record_conflict(
            conn, tenant_id="t1", node_key="产品:洗发水", field="price",
            kept_value="39", kept_source="商品表.xlsx",
            incoming_value="45", incoming_source="促销表.xlsx",
            kept_row_number=88, incoming_row_number=12,
        )
    except BaseException:
        await conn.close()
        raise
    return conn


@pytest.fixture()
def conflicts_conn():
    conn = asyncio.run(_open_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _client(conn, *, graph=None, username: str = "alice", role: str = "admin"):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph or FakeGraph()
    token = session_store.create_session(username=username, role=role, tenant_id=None)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _get_list(conn):
    try:
        client, headers = _client(conn)
        return client.get("/api/admin/t1/conflicts", headers=headers)
    finally:
        app.dependency_overrides.clear()


def _resolve(conn, *, value: str = "45", graph=None, tenant: str = "t1", username: str = "alice"):
    conflict_id = asyncio.run(list_conflicts(conn, tenant_id="t1"))[0]["conflict_id"]
    try:
        client, headers = _client(conn, graph=graph, username=username)
        return client.post(
            f"/api/admin/{tenant}/conflicts/{conflict_id}/resolve",
            json={"value": value}, headers=headers,
        )
    finally:
        app.dependency_overrides.clear()


def _price(conn) -> str:
    async def read():
        cursor = await conn.execute(
            "SELECT extra_properties FROM terms WHERE tenant_id = 't1' AND node_key = '产品:洗发水'"
        )
        row = await cursor.fetchone()
        return json.loads(row[0])["price"]

    return asyncio.run(read())


def test_list_returns_both_values_and_both_sources(conflicts_conn):
    """列表要把两个值和各自来源都给出来。

    只给两个数字的话，审核员没有任何依据判断该信哪个——「哪张表更权威」
    往往就是他做这个决定的全部依据。
    """
    body = _get_list(conflicts_conn).json()

    assert body["total"] == 1
    row = body["conflicts"][0]
    assert (row["kept_value"], row["kept_source"]) == ("39", "商品表.xlsx")
    assert (row["incoming_value"], row["incoming_source"]) == ("45", "促销表.xlsx")


def test_resolving_writes_the_chosen_value_back_into_terms(conflicts_conn):
    """决议不是只改冲突表——选中的值要真的进 terms。

    不写回的话，审核员选完发现实体上的值没变，而系统说「已解决」。
    """
    assert _resolve(conflicts_conn).status_code == 200
    assert _price(conflicts_conn) == "45"
    assert asyncio.run(count_conflicts(conflicts_conn, tenant_id="t1")) == 0


def test_resolving_only_touches_the_field_in_question(conflicts_conn):
    """只改这一个字段，别的原样。

    整份 extra_properties 覆盖过去的话，这次决议之外的字段会被一起改掉
    ——而它们可能刚被另一次导入更新过。
    """
    _resolve(conflicts_conn)

    async def read_all():
        cursor = await conflicts_conn.execute(
            "SELECT extra_properties FROM terms WHERE tenant_id = 't1' AND node_key = '产品:洗发水'"
        )
        return json.loads((await cursor.fetchone())[0])

    assert asyncio.run(read_all()) == {"price": "45", "origin": "日本"}


def test_resolving_also_syncs_the_graph(conflicts_conn):
    """图谱侧也要跟着改。

    只改 SQLite 的话，问答拿到的还是旧值——而审核页显示这条已经处理完了。
    """
    graph = FakeGraph()

    assert _resolve(conflicts_conn, graph=graph).status_code == 200

    assert len(graph.synced) == 1
    # 同步进去的是**改后**的值。同步一个旧值等于没同步，而调用次数看起来
    # 完全正常——只断言"调过一次"的用例分辨不出这两者。
    assert graph.synced[0].extra_properties["price"] == "45"


def test_a_failed_graph_sync_does_not_mark_the_conflict_resolved(conflicts_conn):
    """写图失败时冲突不能标成已决议。

    标了的话它从队列里消失，而图上还是旧值，没有任何地方能再发现它。
    """
    response = _resolve(conflicts_conn, graph=BrokenGraph())

    assert response.status_code == 503
    assert asyncio.run(count_conflicts(conflicts_conn, tenant_id="t1")) == 1
    # 报错要说清 terms 已经改了——不说的话审核员会以为什么都没发生，
    # 而 SQLite 和图谱此刻是不一致的。
    assert "术语表" in response.json()["detail"]


def test_resolve_records_the_real_operator(conflicts_conn):
    """记 session.username，不是写死的常量。

    这是一次人工覆盖机器判断的动作，正是最需要留痕的那种。
    """
    _resolve(conflicts_conn, username="alice")

    row = asyncio.run(list_conflicts(conflicts_conn, tenant_id="t1", status="resolved"))[0]
    assert row["resolved_by"] == "alice"


def test_resolve_accepts_a_third_value(conflicts_conn):
    """两个来源都错时可以手填。

    强制二选一等于逼审核员选一个已知是错的。
    """
    assert _resolve(conflicts_conn, value="39.00 元").status_code == 200
    assert _price(conflicts_conn) == "39.00 元"


async def _open_date_conflict_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    try:
        await ensure_terms_schema(conn)
        await ensure_term_edits_schema(conn)
        await ensure_attribute_conflicts_schema(conn)
        await ensure_ontology_schema(conn)
        await create_tenants_table(conn)
        await ensure_admin_auth_schema(conn)
        await create_admin_user(
            conn, username="alice", password="password1", role="admin", tenant_id=None
        )
        await create_tenant(conn, tenant_id="t1", name="t1")
        await create_term_type(
            conn, tenant_id="t1", value="订单",
            extra_fields=[ExtraFieldSpec(name="purchase_date", value_type="date")],
            actor="alice",
        )
        await confirm_ontology(conn, "t1", actor="alice")
        # 直接插 terms，模拟 ETL 已经写过一次；下面的 record_conflict 模拟
        # 第二次导入给出了不同的日期，这正是终审 C1 指出的可达场景——
        # schema_etl.py 的 `_write_entity_mapping` 恒定传 conflict_conn，
        # 两张进货单表对同一订单的 purchase_date 说法不同就会走到这里。
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, 'etl')",
            ("t1", "订单:A001", "A001", "[]", "订单",
             json.dumps({"purchase_date": "2026-01-05"}, ensure_ascii=False)),
        )
        await conn.commit()
        await record_conflict(
            conn, tenant_id="t1", node_key="订单:A001", field="purchase_date",
            kept_value="2026-01-05", kept_source="进货单A.xlsx",
            incoming_value="2026-02-14", incoming_source="进货单B.xlsx",
            kept_row_number=10, incoming_row_number=20,
        )
    except BaseException:
        await conn.close()
        raise
    return conn


def test_resolving_a_date_conflict_end_to_end_reaches_resolved():
    """终审 C1：ETL 产生的 date 字段冲突要能走完「决议」这条完整链路。

    `_coerce_to_declared_type` 曾经没有 date 分支，任何值都会被
    `ValueError` 拒绝、转成 400——这条冲突会永远卡在 pending 队列里，
    `resolve_conflict`（唯一能把状态改成 resolved 的函数）永远走不到。
    单测那层测的是 `set_extra_property` 本身，钉不住"决议页 400"这个
    后果——这条测试才走的是终审报告点名的那条真实链路：
    HTTP POST /resolve → admin_conflicts_routes → set_extra_property →
    _coerce_to_declared_type。
    """
    conn = asyncio.run(_open_date_conflict_conn())
    try:
        response = _resolve(conn, value="2026-02-14")
        assert response.status_code == 200

        async def read_back():
            cursor = await conn.execute(
                "SELECT extra_properties FROM terms WHERE tenant_id = 't1' "
                "AND node_key = '订单:A001'"
            )
            row = await cursor.fetchone()
            return json.loads(row[0])["purchase_date"]

        assert asyncio.run(read_back()) == "2026-02-14"
        assert asyncio.run(count_conflicts(conn, tenant_id="t1")) == 0
    finally:
        asyncio.run(conn.close())


def test_conflicts_are_scoped_to_the_tenant(conflicts_conn):
    """两个端点都在 tenant_scoped 下——它们会改数据，比列表端点更敏感。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conflicts_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraph()
    try:
        asyncio.run(
            create_admin_user(
                conflicts_conn, username="bob", password="password1",
                role="member", tenant_id="t1",
            )
        )
        token = session_store.create_session(username="bob", role="member", tenant_id=None)
        client = TestClient(app)
        response = client.get(
            "/api/admin/别人的/conflicts", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_resolving_a_conflict_whose_entity_is_gone_says_so(conflicts_conn):
    """实体在冲突记下来之后被删了。

    冲突留在队列里没意义——它指向一个不存在的东西，审核员选什么都写不进去。
    静默成功更糟：他以为定了，而那个值哪儿都没有。
    """

    async def drop_term():
        await conflicts_conn.execute(
            "DELETE FROM terms WHERE tenant_id = 't1' AND node_key = '产品:洗发水'"
        )
        await conflicts_conn.commit()

    asyncio.run(drop_term())

    response = _resolve(conflicts_conn)

    assert response.status_code == 409
    assert "产品:洗发水" in response.json()["detail"]
    assert asyncio.run(count_conflicts(conflicts_conn, tenant_id="t1")) == 1


def test_the_row_numbers_reach_the_http_response(conflicts_conn):
    """两个行号必须真的出现在接口返回的 JSON 里。

    响应模型现在是裸 `list[dict]`，字段是透传的——**没有任何东西钉住这件事**。
    以后有人把它重构成显式字段的 Pydantic 模型（`list[dict]` 看着就像该还的
    债），漏列这两个字段的话 FastAPI 会静默丢掉它们：前端拿到 undefined，
    `row === null` 判false，`undefined.toLocaleString()` 抛 TypeError，
    整张审核卡片崩溃——而没有一条测试会红。

    这是阶段四 C-1 那个形态在 API 层的重演：那次是"生产路径没调用"，
    这次是"生产路径调用了，但没人验证结果真的送到了边界外"。
    """
    body = _get_list(conflicts_conn).json()

    row = body["conflicts"][0]
    assert row["kept_row_number"] == 88
    assert row["incoming_row_number"] == 12
