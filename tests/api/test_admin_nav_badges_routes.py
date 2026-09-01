from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.graphrag.duplicate_review_queue import (
    enqueue_duplicate_suggestion,
    ensure_duplicate_review_schema,
)
from app.graphrag.review_queue import enqueue_for_review, ensure_review_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_duplicate_review_schema(conn)
    await create_tenants_table(conn)
    await create_tenant(conn, tenant_id="demo", name="demo")
    await create_tenant(conn, tenant_id="other", name="other")
    return conn


@pytest.fixture
def review_conn():
    """必须显式 close：aiosqlite 的后台工作线程不是 daemon 线程，泄漏一个
    未关闭的连接会让 pytest 跑完全部用例后卡在解释器退出阶段。做法同
    test_admin_duplicate_review_routes.py。"""
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    return {"Authorization": f"Bearer {session_store.create_session()}"}


async def _seed(conn: aiosqlite.Connection, *, tenant_id: str, relations: int, duplicates: int) -> None:
    for i in range(relations):
        await enqueue_for_review(
            conn,
            subject_candidate=f"甲{i}",
            object_candidate=f"乙{i}",
            relation_type="属于",
            reason="ambiguous",
            source="doc",
            tenant_id=tenant_id,
        )
    for i in range(duplicates):
        await enqueue_duplicate_suggestion(
            conn,
            tenant_id=tenant_id,
            candidate_a_node_key=f"公司:A{i}",
            candidate_b_node_key=f"公司:B{i}",
            similarity_score=0.9,
            reason="alias_overlap",
        )


def _get(review_conn, *, tenant_id: str):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        return client.get(
            "/api/admin/nav-badges",
            params={"tenant_id": tenant_id},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()


def test_returns_both_pending_counts_in_one_call(review_conn):
    """侧边栏两个徽标一次请求拿全。分两次拉的话，导航每次渲染都发两个
    请求，而这两个数是一起显示的，不存在只要其中一个的场景。"""
    asyncio.run(_seed(review_conn, tenant_id="demo", relations=3, duplicates=2))

    response = _get(review_conn, tenant_id="demo")

    assert response.status_code == 200
    assert response.json() == {"pending_relations": 3, "pending_duplicates": 2}


def test_counts_are_scoped_to_the_tenant(review_conn):
    """徽标是给当前租户看的。算进别人的待办会让用户去点一个空列表。"""
    asyncio.run(_seed(review_conn, tenant_id="demo", relations=1, duplicates=1))
    asyncio.run(_seed(review_conn, tenant_id="other", relations=5, duplicates=5))

    assert _get(review_conn, tenant_id="demo").json() == {
        "pending_relations": 1,
        "pending_duplicates": 1,
    }


def test_empty_queues_report_zero_not_an_error(review_conn):
    """没有待办是最常见的状态，不是异常。返回 0 让前端只管渲染。"""
    assert _get(review_conn, tenant_id="demo").json() == {
        "pending_relations": 0,
        "pending_duplicates": 0,
    }


def test_requires_authentication(review_conn):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        response = TestClient(app).get("/api/admin/nav-badges", params={"tenant_id": "demo"})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()
