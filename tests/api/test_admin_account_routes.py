"""账号管理 API。只有 admin 能用。"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app
from tests.settings_factory import build_settings


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await create_tenants_table(conn)
    await ensure_admin_users_schema(conn)
    await create_tenant(conn, tenant_id="demo", name="demo")
    await create_tenant(conn, tenant_id="disabled-one", name="停用的")
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


def _request(
    review_conn,
    method: str,
    path: str,
    *,
    role: str,
    username: str | None = None,
    json=None,
):
    session_store = AdminSessionStore()
    resolved = username or ("admin" if role == "admin" else "alice")
    token = session_store.create_session(
        username=resolved, role=role, tenant_id=None if role == "admin" else "demo"
    )
    app.dependency_overrides[deps.get_settings] = lambda: build_settings(admin_token="tok")
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        return TestClient(app).request(
            method, path, json=json, headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        for dep in (deps.get_settings, deps.get_admin_session_store):
            app.dependency_overrides.pop(dep, None)


def _accounts(review_conn) -> list[dict]:
    return _request(review_conn, "GET", "/api/admin/accounts", role="admin").json()["accounts"]


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


def test_admin_can_list_accounts(review_conn):
    usernames = {a["username"] for a in _accounts(review_conn)}
    assert {"admin", "alice"} <= usernames


def test_response_never_contains_password_hash(review_conn):
    """哈希本身不是密码，但它足够拿去离线爆破。"""
    response = _request(review_conn, "GET", "/api/admin/accounts", role="admin")
    assert "password_hash" not in response.text
    assert "scrypt$" not in response.text


def test_member_cannot_list_accounts(review_conn):
    """账号列表会暴露有哪些租户、每个租户有谁。"""
    assert _request(review_conn, "GET", "/api/admin/accounts", role="member").status_code == 403


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


def test_admin_can_create_a_member(review_conn):
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "bob", "password": "password1", "tenant_id": "demo"},
    )
    assert response.status_code == 201
    assert response.json()["role"] == "member"
    assert response.json()["tenant_id"] == "demo"


def test_created_account_is_always_a_member(review_conn):
    """请求体里塞 role=admin 必须无效。

    本设计不提供"再造一个 admin"的入口——开这个口子会让"不能禁用最后一个
    admin"那条不变量变复杂而收益为零。
    """
    _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={
            "username": "bob",
            "password": "password1",
            "tenant_id": "demo",
            "role": "admin",
        },
    )
    bob = next(a for a in _accounts(review_conn) if a["username"] == "bob")
    assert bob["role"] == "member"


def test_cannot_create_account_for_a_nonexistent_tenant(review_conn):
    """建给不存在的租户，那个账号登录后会看到一片空白，且没人说得出为
    什么。"""
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "bob", "password": "password1", "tenant_id": "ghost"},
    )
    assert response.status_code == 400


def test_cannot_create_account_for_a_disabled_tenant(review_conn):
    from app.graphrag.tenants_store import set_tenant_status

    asyncio.run(set_tenant_status(review_conn, "disabled-one", "disabled"))
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "bob", "password": "password1", "tenant_id": "disabled-one"},
    )
    assert response.status_code == 400


def test_duplicate_username_is_rejected(review_conn):
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "alice", "password": "password1", "tenant_id": "demo"},
    )
    assert response.status_code == 400


def test_reserved_username_is_rejected(review_conn):
    """允许别人叫 admin 会让"最后一个 admin"这件事变得含糊。"""
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "admin", "password": "password1", "tenant_id": "demo"},
    )
    assert response.status_code == 400


def test_too_short_password_is_rejected(review_conn):
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="admin",
        json={"username": "bob", "password": "short", "tenant_id": "demo"},
    )
    assert response.status_code == 400


def test_member_cannot_create_accounts(review_conn):
    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts",
        role="member",
        json={"username": "bob", "password": "password1", "tenant_id": "demo"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 禁用 / 启用
# ---------------------------------------------------------------------------


def test_admin_cannot_disable_itself(review_conn):
    """一次误点就把自己锁在门外，只能手改数据库救。

    断言到**文案**而不只是 400：这里有两条不变量都会返回 400（"不能停用
    自己"和"不能停用最后一个管理员"），只看状态码的话，把前一条删掉测试
    照样绿——它会被后一条兜住。那样这条测试就分辨不出自己在测什么了。
    """
    response = _request(
        review_conn, "POST", "/api/admin/accounts/admin/disable", role="admin"
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "不能停用自己"


def test_last_admin_guard_is_unreachable_today_and_that_is_why_it_stays(review_conn):
    """「不能停用最后一个管理员」这条防线目前**不可达**，有意保留。

    要触发它，需要一个 active 的管理员去停另一个管理员，而后者恰是最后
    一个 active 的——但"后者是最后一个"就意味着操作者自己不是 active
    管理员，那它的 session 在 require_admin_session 那一步已经被拒了。

    这条测试断言的正是这个推理：拿一个已停用的管理员身份去操作，得到的
    是 401（身份被拒）而不是 400（不变量拦截）。写下来是因为下一个读到
    那段 count_active_admins 判断的人会以为它是死代码——它不是，将来开放
    多 admin 或引入其他操作路径时它就会变得可达，那时不必重新想起它。
    """
    from app.auth.admin_users_store import set_admin_user_status

    asyncio.run(
        create_admin_user(
            review_conn, username="root2", password="password1", role="admin", tenant_id=None
        )
    )
    asyncio.run(set_admin_user_status(review_conn, "root2", "disabled"))

    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts/admin/disable",
        role="admin",
        username="root2",
    )

    assert response.status_code == 401


def test_an_active_admin_can_disable_another_admin(review_conn):
    """多管理员时互相停用是允许的——不变量拦的只是"最后一个"。"""
    asyncio.run(
        create_admin_user(
            review_conn, username="root2", password="password1", role="admin", tenant_id=None
        )
    )

    response = _request(
        review_conn,
        "POST",
        "/api/admin/accounts/root2/disable",
        role="admin",
        username="admin",
    )

    assert response.status_code == 200


def test_admin_can_disable_and_enable_a_member(review_conn):
    assert (
        _request(review_conn, "POST", "/api/admin/accounts/alice/disable", role="admin").status_code
        == 200
    )
    alice = next(a for a in _accounts(review_conn) if a["username"] == "alice")
    assert alice["status"] == "disabled"

    assert (
        _request(review_conn, "POST", "/api/admin/accounts/alice/enable", role="admin").status_code
        == 200
    )
    alice = next(a for a in _accounts(review_conn) if a["username"] == "alice")
    assert alice["status"] == "active"


def test_disabling_a_missing_account_is_404_not_silent_success(review_conn):
    """静默成功意味着 admin 以为禁掉了某个人，实际什么也没发生。"""
    response = _request(
        review_conn, "POST", "/api/admin/accounts/nobody/disable", role="admin"
    )
    assert response.status_code == 404


def test_member_cannot_disable_anyone(review_conn):
    response = _request(
        review_conn, "POST", "/api/admin/accounts/alice/disable", role="member"
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 重置密码
# ---------------------------------------------------------------------------


def test_admin_can_reset_a_password_without_the_old_one(review_conn):
    """重置是给"忘了密码"用的。要旧密码就等于不能重置。"""
    response = _request(
        review_conn,
        "PUT",
        "/api/admin/accounts/alice/password",
        role="admin",
        json={"new_password": "password2"},
    )
    assert response.status_code == 200


def test_reset_password_on_a_missing_account_is_404(review_conn):
    response = _request(
        review_conn,
        "PUT",
        "/api/admin/accounts/nobody/password",
        role="admin",
        json={"new_password": "password2"},
    )
    assert response.status_code == 404


def test_member_cannot_reset_anyone_password(review_conn):
    response = _request(
        review_conn,
        "PUT",
        "/api/admin/accounts/alice/password",
        role="member",
        json={"new_password": "password2"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 租户管理也收归 admin
# ---------------------------------------------------------------------------


def test_member_cannot_create_tenants(review_conn):
    """member 建了租户也进不去（它绑死在自己那个上），只会留下一个没人能
    用的空租户。"""
    response = _request(
        review_conn,
        "POST",
        "/api/admin/tenants",
        role="member",
        json={"tenant_id": "newone", "name": "新租户"},
    )
    assert response.status_code == 403


def test_member_cannot_disable_its_own_tenant(review_conn):
    """这条是把租户管理归 admin 而不是归租户作用域校验的理由。

    按租户作用域校验的话，member 对自己所属的 demo 会顺利通过校验，于是
    就能把自己所在的租户停掉。
    """
    response = _request(
        review_conn, "POST", "/api/admin/tenants/demo/disable", role="member"
    )
    assert response.status_code == 403


def test_admin_can_create_tenants(review_conn):
    response = _request(
        review_conn,
        "POST",
        "/api/admin/tenants",
        role="admin",
        json={"tenant_id": "newone", "name": "新租户"},
    )
    assert response.status_code == 201
