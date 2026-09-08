"""租户权限校验。

这是整个账号体系唯一真正的安全边界。改造之前，任何登录者把请求里的
tenant_id 换成别的值就能读写另一个租户——返回 200，没有日志也没有报错。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.session_cookie import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.api.admin_session import AdminSession, AdminSessionStore
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.auth.user_tenants_store import ensure_user_tenants_schema, grant_tenant_access
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
    await ensure_user_tenants_schema(conn)
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


# ---------------------------------------------------------------------------
# 授权判据本身（不经过 HTTP）
#
# 上面那组走完整的 ASGI 栈，钉的是"每一组租户作用域路由都挂上了这道门"。
# 下面这组直接调判据函数，钉的是"门本身的判据是对的"——两者都要有：只有
# 上面那组时，判据里任何一条分支写错都得先凑出一条恰好触发它的路由才看得见。
# ---------------------------------------------------------------------------


async def _grants_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await ensure_user_tenants_schema(conn)
    return conn


def _session(username: str, role: str, tenant_id: str | None) -> AdminSession:
    return AdminSession(
        username=username,
        role=role,
        tenant_id=tenant_id,
        expires_at=1e18,
        current_tenant_id=tenant_id,
    )


def test_admin_can_reach_every_tenant():
    """admin 得能进入自己刚新建的租户，否则建完就管不了。None 表示
    「不设限」，不是「一个都没有」。"""

    async def run():
        conn = await _grants_conn()
        try:
            session = _session("root", "admin", None)
            assert await deps.list_accessible_tenant_ids(conn, session) is None
            await deps.assert_tenant_accessible(conn, session, "从来没授权过的租户")
        finally:
            await conn.close()

    asyncio.run(run())


def test_member_reaches_exactly_what_was_granted():
    async def run():
        conn = await _grants_conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            await grant_tenant_access(conn, username="bob", tenant_id="secret")
            session = _session("alice", "member", "muji-goods")
            assert await deps.list_accessible_tenant_ids(conn, session) == [
                "muji-goods",
                "muji-store",
            ]
            await deps.assert_tenant_accessible(conn, session, "muji-store")
        finally:
            await conn.close()

    asyncio.run(run())


def test_member_is_refused_a_tenant_someone_else_was_granted():
    """这是整个账号体系唯一真正的安全边界。bob 有 secret 的授权不等于
    alice 有——「表里存在这条 tenant_id」和「这个人有它」是两回事。"""

    async def run():
        conn = await _grants_conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="bob", tenant_id="secret")
            session = _session("alice", "member", "muji-goods")
            with pytest.raises(HTTPException) as excinfo:
                await deps.assert_tenant_accessible(conn, session, "secret")
            assert excinfo.value.status_code == 403
        finally:
            await conn.close()

    asyncio.run(run())


def test_member_with_no_grants_reaches_nothing_not_everything():
    """一条授权都没有、且 tenant_id 那一列也是空时返回空列表，不是 None。
    返回 None 的话它会被 admin 分支的语义吞掉，变成「不设限」——一个没有
    任何授权的账号因此能读写所有租户。"""

    async def run():
        conn = await _grants_conn()
        try:
            session = _session("nobody", "member", None)
            assert await deps.list_accessible_tenant_ids(conn, session) == []
            with pytest.raises(HTTPException) as excinfo:
                await deps.assert_tenant_accessible(conn, session, "muji-goods")
            assert excinfo.value.status_code == 403
        finally:
            await conn.close()

    asyncio.run(run())


def test_legacy_member_falls_back_to_their_own_tenant_id_column():
    """存量 member 在 user_tenants 里一条记录都没有（这张表刚建）。
    此时必须回退到 admin_users.tenant_id，否则这次升级会把所有现存
    member 一次性锁在门外——而他们昨天还能正常工作。

    回退只认自己那一个，不是放行全部。"""

    async def run():
        conn = await _grants_conn()
        try:
            session = _session("legacy", "member", "muji-goods")
            assert await deps.list_accessible_tenant_ids(conn, session) == ["muji-goods"]
            await deps.assert_tenant_accessible(conn, session, "muji-goods")
            with pytest.raises(HTTPException):
                await deps.assert_tenant_accessible(conn, session, "别人的租户")
        finally:
            await conn.close()

    asyncio.run(run())


def test_explicit_grants_replace_the_fallback_they_do_not_add_to_it():
    """一旦有了显式授权，就完全以显式授权为准。把 tenant_id 那一列并进来
    的话，「撤销 alice 对 muji-goods 的访问」这个操作永远生效不了——
    她的默认租户还在那一列里挂着。"""

    async def run():
        conn = await _grants_conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            session = _session("alice", "member", "muji-goods")
            assert await deps.list_accessible_tenant_ids(conn, session) == ["muji-store"]
            with pytest.raises(HTTPException):
                await deps.assert_tenant_accessible(conn, session, "muji-goods")
        finally:
            await conn.close()

    asyncio.run(run())


def test_grant_lookup_does_not_depend_on_a_row_factory_someone_else_set():
    """判据自己负责 row_factory。

    生产路径上 require_admin_session 先调 get_admin_user，那个函数把
    row_factory 设成了 aiosqlite.Row，于是授权查询"碰巧"能按列名取值。
    依赖那个顺序的话，任何一条先到达判据的调用路径都会以 TypeError
    （500）而不是 403 收场——安全边界不该有这种取决于调用顺序的行为。
    """

    async def run():
        conn = await aiosqlite.connect(":memory:")
        try:
            await ensure_user_tenants_schema(conn)
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            session = _session("alice", "member", None)
            assert await deps.list_accessible_tenant_ids(conn, session) == ["muji-store"]
        finally:
            await conn.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 切租户路由走的是同一道门
#
# 这一组存在的唯一理由：判据此前在 deps.require_tenant_access 和切租户路由
# 里各写了一遍。只改其中一处，读写接口和切租户接口就会给出互相矛盾的答案，
# 而两边各自的用例都还是绿的。下面两条一正一反，钉的正是"两处是同一处"。
# ---------------------------------------------------------------------------


def _switch_tenant(review_conn, *, username: str, target: str):
    """走完整的 Cookie 登录再切租户。

    不能用 Bearer：这条路由只从 Cookie 里取 token（`set_current_tenant` 要
    改的就是那个 Cookie 会话），Bearer 调用方一律拿 401。用 Bearer 的话
    "允许切进去"那条正面用例会在 401 上失败，而"拒绝"那条会因为 403 先
    发生而假绿——它压根没走到判据后面。
    """
    # 同一个 store 实例贯穿登录和切换：`lambda: AdminSessionStore()` 每次
    # 解析依赖都新建一个，登录发出的 token 到下一个请求就查不到了，全部 401。
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(admin_token="tok")
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        login = client.post(
            "/api/admin/auth/login", json={"username": username, "password": "password1"}
        )
        assert login.status_code == 200, login.text
        return client.put(
            "/api/admin/auth/session/tenant",
            json={"tenant_id": target},
            headers={CSRF_HEADER_NAME: client.cookies.get(CSRF_COOKIE_NAME)},
        )
    finally:
        for dep in (deps.get_settings, deps.get_admin_session_store):
            app.dependency_overrides.pop(dep, None)


def test_switch_tenant_refuses_a_tenant_the_grants_no_longer_include(review_conn):
    """alice 的 admin_users.tenant_id 仍是 demo，但显式授权只剩 other——
    也就是"管理员刚刚撤销了她对 demo 的访问"。切租户接口必须跟读写接口
    给出同一个答案：403。

    只改了 deps 那一处的实现会在这里返回 200：切得过去，进去之后每个
    读写请求再各自 403。用户看到的是一个进得去、什么都打不开的租户。
    """
    asyncio.run(grant_tenant_access(review_conn, username="alice", tenant_id="other"))
    response = _switch_tenant(review_conn, username="alice", target="demo")
    assert response.status_code == 403


def test_switch_tenant_allows_a_tenant_only_the_grants_confer(review_conn):
    """反面：授权真的能让她切进一个不属于她那一列的租户。

    没有这一条，把切租户路由写成"一律 403"也能让上面那条绿。
    """
    asyncio.run(grant_tenant_access(review_conn, username="alice", tenant_id="other"))
    response = _switch_tenant(review_conn, username="alice", target="other")
    assert response.status_code == 200
