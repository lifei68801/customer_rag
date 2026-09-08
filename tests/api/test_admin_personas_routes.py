"""GET /api/admin/personas ——「我这个账号能访问哪些数字人」。

这是一个非租户路径（路径里没有 {tenant_id}），也是这个账号体系里少数
几个不挂 require_admin_role 的端点之一：右栏对所有角色都要出现，安全性
完全靠返回内容按 list_accessible_tenant_ids 过滤。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.auth.user_tenants_store import ensure_user_tenants_schema, grant_tenant_access
from app.graphrag.tenant_personas_store import (
    ensure_tenant_personas_schema,
    upsert_persona,
)
from app.graphrag.tenants_store import create_tenant, create_tenants_table, set_tenant_status
from app.main import app
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


async def _open_personas_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_admin_users_schema(conn)
    await ensure_user_tenants_schema(conn)
    await create_tenants_table(conn)
    await ensure_tenant_personas_schema(conn)

    await create_admin_user(conn, username="root", password="password1", role="admin", tenant_id=None)
    # admin_users 上的 CHECK 约束要求 member 必须有非空 tenant_id（这一列是
    # 存量回退值，见 user_tenants_store.py 顶部注释）。alice 在 user_tenants
    # 里有显式授权，这一列的具体取值不影响本文件任何用例的判定。
    await create_admin_user(conn, username="alice", password="password1", role="member", tenant_id="muji-goods")

    # 三个租户：muji-goods、muji-store 都建了 persona，secret 也建了但 alice
    # 无权访问——它必须**完全不出现**在 alice 的结果里，多列一个都是越权
    # （即使点进去会被 403 挡住，名字本身已经泄露"这家公司还有一个叫机密的
    # 领域"）。muji-store 故意不配脸，用来钉住"没配脸的租户仍要出现，
    # 只是 avatar/tagline 为空、name 退回租户名"。
    for tenant_id, name in (("muji-goods", "杂货"), ("muji-store", "门店"), ("secret", "机密")):
        await create_tenant(conn, tenant_id=tenant_id, name=name)

    # alice 只被授权前两个，secret 对她不可见。
    await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
    await grant_tenant_access(conn, username="alice", tenant_id="muji-store")

    await upsert_persona(conn, tenant_id="muji-goods", avatar="https://x/goods.png", tagline="欢迎光临杂货部")
    await upsert_persona(conn, tenant_id="secret", avatar="https://x/secret.png", tagline="机密频道")
    # muji-store 故意不调用 upsert_persona。

    return conn


@pytest.fixture
def personas_conn():
    """独立的 :memory: 连接，每个用例都拿到全新的种子数据（路由层依赖的是
    deps.get_review_conn，这里用 dependency_overrides 整个替换掉）。"""
    conn = asyncio.run(_open_personas_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _get_personas_raw(conn: aiosqlite.Connection, *, headers: dict[str, str]):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    try:
        client = TestClient(app)
        return client.get("/api/admin/personas", headers=headers)
    finally:
        app.dependency_overrides.clear()


def _get_personas(conn: aiosqlite.Connection, *, username: str, role: str):
    """以指定账号登录后请求 /api/admin/personas，返回响应体（已断言 200）。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conn
    try:
        token = session_store.create_session(username=username, role=role, tenant_id=None)
        client = TestClient(app)
        response = client.get(
            "/api/admin/personas", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    return response.json()


def test_member_sees_only_the_personas_they_were_granted(personas_conn):
    """右栏只列这个账号有权访问的。多列一个都是越权——即使他点进去会被
    403 挡住，那个名字本身就已经泄露了「这家公司还有一个叫供应链的领域」。"""
    body = _get_personas(personas_conn, username="alice", role="member")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods", "muji-store"]


def test_admin_sees_every_active_tenant(personas_conn):
    """admin 不设限——它得能进入自己刚新建的租户。"""
    body = _get_personas(personas_conn, username="root", role="admin")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods", "muji-store", "secret"]


def test_disabled_tenants_do_not_appear(personas_conn):
    """停用的租户不列：切过去之后所有写操作都是 404，而用户不知道为什么。
    这一条钉的是「授权还在但租户停用了」这个组合。"""
    asyncio.run(set_tenant_status(personas_conn, "muji-store", "disabled"))
    body = _get_personas(personas_conn, username="alice", role="member")
    assert [p["tenant_id"] for p in body["personas"]] == ["muji-goods"]


def test_a_tenant_without_a_persona_still_appears_with_its_tenant_name(personas_conn):
    """没配过脸的租户照样出现在右栏，只是 avatar/tagline 为空。
    不出现的话，管理员新建一个租户、授权给用户，用户却看不见它——
    而没有任何地方告诉他「你需要先去配一张脸」。"""
    body = _get_personas(personas_conn, username="alice", role="member")
    unconfigured = next(p for p in body["personas"] if p["tenant_id"] == "muji-store")
    assert unconfigured["avatar"] == ""
    assert unconfigured["tagline"] == ""
    assert unconfigured["name"] == "门店"  # 退回租户名，不是空字符串
    # 顺带确认配了脸的那个不会被这条断言误判成"也是空的"。
    configured = next(p for p in body["personas"] if p["tenant_id"] == "muji-goods")
    assert configured["avatar"] == "https://x/goods.png"
    assert configured["tagline"] == "欢迎光临杂货部"


def test_anonymous_request_is_refused(personas_conn):
    """不带会话的请求得 401。右栏列的是「你能访问哪些知识库」，
    这个问题对匿名者没有答案。"""
    assert _get_personas_raw(personas_conn, headers={}).status_code == 401
