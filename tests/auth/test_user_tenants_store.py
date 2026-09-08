import asyncio

import aiosqlite

from app.auth.user_tenants_store import (
    ensure_user_tenants_schema,
    grant_tenant_access,
    list_granted_tenant_ids,
    list_usernames_with_access,
    revoke_tenant_access,
)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await ensure_user_tenants_schema(conn)
    return conn


def test_grant_and_list_by_user():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            # bob 的授权不该出现在 alice 名下。少了这一条，「返回全表」的
            # 实现也能让上面两条变绿。
            await grant_tenant_access(conn, username="bob", tenant_id="other")
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-goods", "muji-store"]
            assert await list_granted_tenant_ids(conn, "bob") == ["other"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_grant_is_idempotent():
    """重复授权不该报错、也不该在列表里出现两次。管理员点两下按钮是常态。"""

    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_revoke_only_removes_that_one_pair():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="alice", tenant_id="muji-store")
            await grant_tenant_access(conn, username="bob", tenant_id="muji-goods")
            await revoke_tenant_access(conn, username="alice", tenant_id="muji-goods")
            # 三个方向都要钉：alice 少了那一个、alice 另一个还在、bob 没被牵连。
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-store"]
            assert await list_granted_tenant_ids(conn, "bob") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_revoking_a_grant_that_never_existed_is_a_noop_not_an_error():
    """撤销一条不存在的授权不报错：并发下两个管理员同时点撤销，第二个人
    看到 500 会以为撤销失败、然后反复重试。"""

    async def run():
        conn = await _conn()
        try:
            await revoke_tenant_access(conn, username="alice", tenant_id="从未授权")
            assert await list_granted_tenant_ids(conn, "alice") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_list_by_tenant():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="bob", tenant_id="muji-goods")
            await grant_tenant_access(conn, username="carol", tenant_id="other")
            assert await list_usernames_with_access(conn, "muji-goods") == ["alice", "bob"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_ensure_schema_is_idempotent_and_keeps_grants():
    async def run():
        conn = await _conn()
        try:
            await grant_tenant_access(conn, username="alice", tenant_id="muji-goods")
            await ensure_user_tenants_schema(conn)
            assert await list_granted_tenant_ids(conn, "alice") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())
