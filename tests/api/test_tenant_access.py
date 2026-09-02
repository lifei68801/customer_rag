"""租户权限校验。

这是整个账号体系唯一真正的安全边界。改造之前，任何登录者把请求里的
tenant_id 换成别的值就能读写另一个租户——返回 200，没有日志也没有报错。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.graphrag.duplicate_review_queue import ensure_duplicate_review_schema
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app
from tests.settings_factory import build_settings


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_duplicate_review_schema(conn)
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await create_tenants_table(conn)
    await ensure_admin_users_schema(conn)
    await create_tenant(conn, tenant_id="demo", name="demo")
    await create_tenant(conn, tenant_id="other", name="other")
    await create_admin_user(
        conn, username="admin", password="password1", role="admin", tenant_id=None
    )
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    return conn


@pytest.fixture
def review_conn():
    """必须显式 close：aiosqlite 的后台工作线程不是 daemon 线程，泄漏一个
    未关闭的连接会让 pytest 跑完全部用例后卡在解释器退出阶段。"""
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _get(review_conn, *, path: str, username: str, role: str, tenant_id: str | None):
    session_store = AdminSessionStore()
    token = session_store.create_session(username=username, role=role, tenant_id=tenant_id)
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(admin_token="tok")
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        return TestClient(app).get(path, headers={"Authorization": f"Bearer {token}"})
    finally:
        for dep in (deps.get_settings, deps.get_admin_session_store):
            app.dependency_overrides.pop(dep, None)


def _as_member(review_conn, path: str):
    return _get(review_conn, path=path, username="alice", role="member", tenant_id="demo")


def _as_admin(review_conn, path: str):
    return _get(review_conn, path=path, username="admin", role="admin", tenant_id=None)


def test_member_can_read_own_tenant(review_conn):
    assert _as_member(review_conn, "/api/admin/demo/nav-badges").status_code == 200


def test_member_cannot_read_another_tenant(review_conn):
    """这是整个改造的核心断言。改造前这个请求返回 200 和别人的数据。"""
    assert _as_member(review_conn, "/api/admin/other/nav-badges").status_code == 403


def test_admin_can_read_any_tenant(review_conn):
    """admin 得能进入自己新建的租户，否则建完就管不了。"""
    for tenant in ("demo", "other"):
        assert _as_admin(review_conn, f"/api/admin/{tenant}/nav-badges").status_code == 200


#: 每一组租户作用域路由都要验一遍。挂载层漏了哪一组，这里就红哪一条——
#: 而漏掉的那组在生产上不会有任何报错，请求照常 200，只是返回别人的数据。
_TENANT_SCOPED_PROBES = [
    "/api/admin/other/nav-badges",
    "/api/admin/other/terms",
    "/api/admin/other/documents",
    "/api/admin/other/graph-reviews",
    "/api/admin/other/duplicate-reviews",
    "/api/admin/other/diagnostics",
    "/api/admin/ontology/other/status",
    "/api/admin/other/schema-etl/status",
]


@pytest.mark.parametrize("path", _TENANT_SCOPED_PROBES)
def test_every_tenant_route_group_blocks_cross_tenant_access(review_conn, path: str):
    assert _as_member(review_conn, path).status_code == 403


@pytest.mark.parametrize("path", _TENANT_SCOPED_PROBES)
def test_admin_is_not_blocked_on_any_group(review_conn, path: str):
    """反面：403 必须来自权限判断，不是因为这条路由整个坏了。

    没有这一条，把 require_tenant_access 写成"一律 403"也能让上面那组
    全绿。
    """
    assert _as_admin(review_conn, path).status_code != 403


def test_login_is_not_broken_by_the_tenant_dependency(review_conn):
    """登录接口不该被卷进租户校验。挂上去的话 FastAPI 会把 tenant_id 当成
    必填查询参数，登录直接 422——那时谁也进不来。"""
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(admin_token="tok")
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        response = TestClient(app).post(
            "/api/admin/auth/login", json={"username": "alice", "password": "password1"}
        )
    finally:
        for dep in (deps.get_settings, deps.get_admin_session_store):
            app.dependency_overrides.pop(dep, None)

    assert response.status_code == 200
