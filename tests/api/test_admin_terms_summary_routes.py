from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from tests.settings_factory import build_settings

# 按类型分组的摘要。
#
# 实体列表默认第一页永远是按 standard_name 排序的前 50 个订单号——对任何
# 任务都没用。20017 条里有 20000 条是订单号和用户名这类事实型实体，它们的
# 正确性由 ETL 映射规则保证，逐条看没有收益；剩下 17 条维度实体才需要人看，
# 而 17 条根本不需要分页。


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


@pytest.fixture
def review_conn():
    """必须显式 close：aiosqlite 的后台线程不是 daemon，泄漏连接会让 pytest
    跑完全部用例后卡在解释器退出阶段。"""
    async def _open():
        conn = await aiosqlite.connect(":memory:")
        await ensure_terms_schema(conn)
        await ensure_term_edits_schema(conn)
        await ensure_ontology_schema(conn)
        await create_tenants_table(conn)
        await create_tenant(conn, tenant_id="demo", name="demo")
        return conn

    conn = asyncio.run(_open())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def _seed(conn, term_type: str, count: int) -> None:
    for i in range(count):
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type,"
            " extra_properties, source) VALUES ('demo', ?, ?, '[]', ?, '{}', 'etl')",
            (f"{term_type}:{i}", f"{term_type}-{i}", term_type),
        )
    await conn.commit()


def _get(review_conn, path: str):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        token = session_store.create_session()
        return client.get(path, headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()


def test_summary_groups_by_type_largest_first(review_conn):
    """大类型排前面：它们是最可能出问题的地方（一条映射规则错了就是上万条
    错），而小类型人扫一眼就看完了。"""
    asyncio.run(_seed(review_conn, "订单号", 20))
    asyncio.run(_seed(review_conn, "公司", 3))

    body = _get(review_conn, "/api/admin/demo/terms/summary").json()

    assert body["groups"] == [
        {"term_type": "订单号", "total": 20},
        {"term_type": "公司", "total": 3},
    ]


def test_summary_is_empty_for_a_fresh_tenant(review_conn):
    assert _get(review_conn, "/api/admin/demo/terms/summary").json()["groups"] == []


def test_list_filters_by_term_type(review_conn):
    """小基数类型直接列全部，需要按类型取。"""
    asyncio.run(_seed(review_conn, "公司", 3))
    asyncio.run(_seed(review_conn, "订单号", 5))

    body = _get(review_conn, "/api/admin/demo/terms?term_type=公司").json()

    assert body["total"] == 3
    assert {t["term_type"] for t in body["terms"]} == {"公司"}


def test_type_filter_combines_with_paging(review_conn):
    asyncio.run(_seed(review_conn, "订单号", 10))

    body = _get(review_conn, "/api/admin/demo/terms?term_type=订单号&page=1&page_size=4").json()

    assert len(body["terms"]) == 4
    assert body["total"] == 10


def test_summary_route_does_not_shadow_the_detail_route(review_conn):
    """/terms/summary 和 /terms/{node_key} 长得一样。顺序错了的话，
    详情页对所有实体都会 404 或者返回一份摘要。"""
    asyncio.run(_seed(review_conn, "公司", 1))

    detail = _get(review_conn, "/api/admin/demo/terms/公司:0")

    assert detail.status_code == 200
    assert detail.json()["standard_name"] == "公司-0"
