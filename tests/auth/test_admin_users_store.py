from __future__ import annotations

import aiosqlite
import pytest

from app.auth.admin_users_store import (
    AdminUserAlreadyExistsError,
    AdminUserNotFoundError,
    InvalidUsernameError,
    count_active_admins,
    create_admin_user,
    ensure_admin_users_schema,
    get_admin_user,
    list_admin_users,
    set_admin_user_password,
    set_admin_user_status,
    touch_last_login,
)
from app.auth.password import verify_password


@pytest.fixture
async def conn():
    """必须显式 close：aiosqlite 的后台工作线程不是 daemon 线程，泄漏一个
    未关闭的连接会让 pytest 跑完全部用例后卡在解释器退出阶段。做法同
    tests/api/test_admin_nav_badges_routes.py。"""
    connection = await aiosqlite.connect(":memory:")
    await ensure_admin_users_schema(connection)
    try:
        yield connection
    finally:
        await connection.close()


async def test_created_user_can_be_read_back(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    user = await get_admin_user(conn, "alice")
    assert user is not None
    assert user["username"] == "alice"
    assert user["role"] == "member"
    assert user["tenant_id"] == "demo"
    assert user["status"] == "active"


async def test_password_is_stored_hashed_not_plaintext(conn):
    """明文存密码是不可原谅的。这条断言的是存储值既不等于明文、又能校验
    通过——只断言"不等于明文"的话，存一个 md5 也能过。"""
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    user = await get_admin_user(conn, "alice")
    assert user["password_hash"] != "password1"
    assert verify_password("password1", user["password_hash"]) is True


async def test_missing_user_returns_none_not_error(conn):
    assert await get_admin_user(conn, "nobody") is None


async def test_duplicate_username_is_rejected(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    with pytest.raises(AdminUserAlreadyExistsError):
        await create_admin_user(
            conn, username="alice", password="password2", role="member", tenant_id="other"
        )


@pytest.mark.parametrize("username", ["ab", "x" * 33, "has space", "有中文", "a@b", ""])
async def test_invalid_username_is_rejected(conn, username: str):
    with pytest.raises(InvalidUsernameError):
        await create_admin_user(
            conn, username=username, password="password1", role="member", tenant_id="demo"
        )


async def test_boundary_length_usernames_are_accepted(conn):
    """边界：3 和 32 位都要能过。差一位就把人挡在门外。"""
    for username in ("abc", "x" * 32):
        await create_admin_user(
            conn, username=username, password="password1", role="member", tenant_id="demo"
        )
        assert await get_admin_user(conn, username) is not None


async def test_admin_must_not_have_a_tenant(conn):
    """admin 是全局的。给它绑一个租户会让"admin 能看所有租户"这件事变得
    含糊——到底看全部，还是只看绑的那个？"""
    with pytest.raises(AdminUserAlreadyExistsError):
        await create_admin_user(
            conn, username="root", password="password1", role="admin", tenant_id="demo"
        )


async def test_member_must_have_a_tenant(conn):
    """没有租户的 member 是个看不到任何数据的账号——建出来就是个陷阱。"""
    with pytest.raises(AdminUserAlreadyExistsError):
        await create_admin_user(
            conn, username="alice", password="password1", role="member", tenant_id=None
        )


async def test_list_never_exposes_password_hash(conn):
    """列表接口会直接返回给前端。哈希值本身不是密码，但它足够离线爆破。"""
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    users = await list_admin_users(conn)
    assert users
    for user in users:
        assert "password_hash" not in user


async def test_status_can_be_toggled(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    await set_admin_user_status(conn, "alice", "disabled")
    assert (await get_admin_user(conn, "alice"))["status"] == "disabled"
    await set_admin_user_status(conn, "alice", "active")
    assert (await get_admin_user(conn, "alice"))["status"] == "active"


async def test_status_change_on_missing_user_raises(conn):
    """静默成功是最糟的结果：admin 点了"禁用"，界面说成功，那个账号还能
    登录。"""
    with pytest.raises(AdminUserNotFoundError):
        await set_admin_user_status(conn, "nobody", "disabled")


async def test_password_can_be_changed(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    await set_admin_user_password(conn, "alice", "password2")
    stored = (await get_admin_user(conn, "alice"))["password_hash"]
    assert verify_password("password2", stored) is True
    assert verify_password("password1", stored) is False


async def test_password_change_on_missing_user_raises(conn):
    with pytest.raises(AdminUserNotFoundError):
        await set_admin_user_password(conn, "nobody", "password2")


async def test_last_login_starts_empty_and_gets_set(conn):
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    assert (await get_admin_user(conn, "alice"))["last_login_at"] is None
    await touch_last_login(conn, "alice")
    assert (await get_admin_user(conn, "alice"))["last_login_at"] is not None


async def test_counts_only_active_admins(conn):
    """这个数是"不能禁用最后一个 admin"那条不变量的依据。把 disabled 的
    也算进去，就会允许把最后一个可用 admin 也禁掉。"""
    await create_admin_user(
        conn, username="root", password="password1", role="admin", tenant_id=None
    )
    assert await count_active_admins(conn) == 1
    await set_admin_user_status(conn, "root", "disabled")
    assert await count_active_admins(conn) == 0


async def test_members_are_not_counted_as_admins(conn):
    """只数 admin。把 member 算进去会让"最后一个管理员"永远算不到 0，
    那条不变量就形同虚设。"""
    await create_admin_user(
        conn, username="alice", password="password1", role="member", tenant_id="demo"
    )
    assert await count_active_admins(conn) == 0


async def test_schema_is_idempotent(conn):
    """启动时每次都会调用。第二次调用报错会让进程起不来。"""
    await ensure_admin_users_schema(conn)
    await ensure_admin_users_schema(conn)
