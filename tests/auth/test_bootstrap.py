from __future__ import annotations

import aiosqlite
import pytest

from app.auth.admin_users_store import (
    ensure_admin_users_schema,
    get_admin_user,
    set_admin_user_password,
)
from app.auth.bootstrap import (
    STALE_TEST_TENANTS,
    AdminSeedError,
    disable_stale_test_tenants,
    seed_admin_user,
)
from app.auth.password import verify_password
from app.graphrag.tenants_store import (
    create_tenant,
    create_tenants_table,
    list_tenants,
    set_tenant_status,
)


@pytest.fixture
async def conn():
    """必须显式 close：aiosqlite 的后台工作线程不是 daemon 线程，泄漏一个
    未关闭的连接会让 pytest 跑完全部用例后卡在解释器退出阶段。"""
    connection = await aiosqlite.connect(":memory:")
    await ensure_admin_users_schema(connection)
    await create_tenants_table(connection)
    try:
        yield connection
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# 播种 admin
# ---------------------------------------------------------------------------


async def test_seeds_admin_when_table_is_empty(conn):
    assert await seed_admin_user(conn, "initialsecret") is True

    user = await get_admin_user(conn, "admin")
    assert user["role"] == "admin"
    assert user["tenant_id"] is None
    assert verify_password("initialsecret", user["password_hash"]) is True


async def test_does_not_overwrite_an_existing_admin(conn):
    """改密后重启不能被环境变量覆盖回去——否则"改密码"这个功能是假的。"""
    await seed_admin_user(conn, "initialsecret")
    await set_admin_user_password(conn, "admin", "changedlater")

    assert await seed_admin_user(conn, "initialsecret") is False

    stored = (await get_admin_user(conn, "admin"))["password_hash"]
    assert verify_password("changedlater", stored) is True
    assert verify_password("initialsecret", stored) is False


async def test_empty_token_with_no_admin_raises(conn):
    """启动成功但无人能登录是最坏的形态——运维会以为是自己记错了密码，
    而不是去看配置。所以这里让进程起不来。"""
    with pytest.raises(AdminSeedError):
        await seed_admin_user(conn, None)


async def test_empty_token_with_existing_admin_is_fine(conn):
    """admin 已存在时，这个环境变量已经完成使命，是否为空无关紧要。"""
    await seed_admin_user(conn, "initialsecret")

    assert await seed_admin_user(conn, None) is False


async def test_reseeds_after_the_admin_row_is_wiped(conn):
    """清空 admin_users 后重启会重新播种——这是文档里写的那条恢复路径，
    admin 忘了密码时唯一的出路。"""
    await seed_admin_user(conn, "initialsecret")
    await conn.execute("DELETE FROM admin_users")
    await conn.commit()

    assert await seed_admin_user(conn, "initialsecret") is True
    assert await get_admin_user(conn, "admin") is not None


# ---------------------------------------------------------------------------
# 测试残留租户
# ---------------------------------------------------------------------------


async def test_disables_only_the_stale_test_tenants(conn):
    for tenant_id in ["demo", "default", *STALE_TEST_TENANTS]:
        await create_tenant(conn, tenant_id=tenant_id, name=tenant_id)

    disabled = await disable_stale_test_tenants(conn)

    assert set(disabled) == set(STALE_TEST_TENANTS)
    by_id = {t["tenant_id"]: t for t in await list_tenants(conn, include_disabled=True)}
    # 真实租户一个都不能动。
    assert by_id["demo"]["status"] == "active"
    assert by_id["default"]["status"] == "active"
    for tenant_id in STALE_TEST_TENANTS:
        assert by_id[tenant_id]["status"] == "disabled"


async def test_second_run_reports_nothing_new(conn):
    """迁移每次启动都会跑。第二次不该再报"我禁用了这些"——那会让日志每次
    启动都刷一遍已经完成的事。"""
    for tenant_id in STALE_TEST_TENANTS:
        await create_tenant(conn, tenant_id=tenant_id, name=tenant_id)

    assert set(await disable_stale_test_tenants(conn)) == set(STALE_TEST_TENANTS)
    assert await disable_stale_test_tenants(conn) == []


async def test_missing_tenants_are_skipped_not_created(conn):
    """全新部署里这些租户根本不存在。禁用一个不存在的租户不该报错，更不该
    把它建出来。"""
    assert await disable_stale_test_tenants(conn) == []
    assert await list_tenants(conn, include_disabled=True) == []


async def test_a_manually_reenabled_tenant_gets_disabled_again(conn):
    """已知行为，写下来是因为它会让人意外：手动启用某个残留租户之后，下次
    启动会再次禁用它。要长期保留其中某个，得改 STALE_TEST_TENANTS 常量。"""
    await create_tenant(conn, tenant_id=STALE_TEST_TENANTS[0], name="x")
    await disable_stale_test_tenants(conn)
    await set_tenant_status(conn, STALE_TEST_TENANTS[0], "active")

    assert await disable_stale_test_tenants(conn) == [STALE_TEST_TENANTS[0]]


async def test_too_short_token_reports_which_setting_to_fix(conn):
    """密码长度错误必须翻译成"改哪个环境变量"。

    原始异常只说"密码至少 8 个字符"，而这里根本没有"用户输入的密码"这回事
    ——值来自环境变量。不翻译的话，运维看到进程起不来、日志说密码太短，
    会满世界找哪个表单提交了短密码。

    这条在升级既有部署时会真的撞上：旧的 CUSTOMER_RAG_ADMIN_TOKEN 没有长度
    要求，短于 8 位的完全可能。
    """
    with pytest.raises(AdminSeedError) as exc_info:
        await seed_admin_user(conn, "short")

    assert "CUSTOMER_RAG_ADMIN_TOKEN" in str(exc_info.value)
