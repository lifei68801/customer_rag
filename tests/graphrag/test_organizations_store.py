import asyncio

import aiosqlite
import pytest

from app.graphrag.organizations_store import (
    OrganizationAlreadyExistsError,
    OrganizationNotFoundError,
    assign_tenant_to_org,
    create_organization,
    ensure_organizations_schema,
    list_organizations,
    list_tenants_in_org,
)
from app.graphrag.tenants_store import (
    TenantNotFoundError,
    create_tenant,
    create_tenants_table,
)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_tenants_table(conn)
    await ensure_organizations_schema(conn)
    return conn


def test_create_and_list_organizations():
    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_organization(conn, org_id="acme", name="ACME")
            rows = await list_organizations(conn)
            assert [r["org_id"] for r in rows] == ["acme", "muji"]
            assert [r["name"] for r in rows] == ["ACME", "无印良品"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_duplicate_org_id_is_rejected_not_silently_merged():
    """同一个 org_id 建两次要报错。静默合并的话，第二次那个「新组织」
    的名字会覆盖第一个的，而建它的人以为自己建了个新的。"""

    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            with pytest.raises(OrganizationAlreadyExistsError):
                await create_organization(conn, org_id="muji", name="另一家公司")
            rows = await list_organizations(conn)
            assert [r["name"] for r in rows] == ["无印良品"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_assign_tenant_to_org_and_list_back():
    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            await create_tenant(conn, tenant_id="muji-store", name="门店")
            await create_tenant(conn, tenant_id="other", name="别家")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")
            await assign_tenant_to_org(conn, tenant_id="muji-store", org_id="muji")
            # 第三个租户没分配——它不该出现在 muji 名下。全都返回的实现
            # 和正确实现都能让「两个都在」的断言变绿，所以这一条必须有。
            assert await list_tenants_in_org(conn, "muji") == ["muji-goods", "muji-store"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_assigning_to_a_nonexistent_org_is_rejected():
    """挂到一个不存在的组织下要报错。放行的话，这个租户会从组织视图里
    彻底消失——它有 org_id，但那个 org_id 谁也查不到。"""

    async def run():
        conn = await _conn()
        try:
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            with pytest.raises(OrganizationNotFoundError):
                await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="不存在")
        finally:
            await conn.close()

    asyncio.run(run())


def test_assigning_a_nonexistent_tenant_is_rejected():
    """tenant_id 不存在时要报错，而不是静默影响 0 行。UPDATE 语句本身
    对"没有匹配行"这件事不会报任何错——不校验的话，管理员把 tenant_id
    拼错也会拿到"成功"，而库里其实什么都没发生。"""

    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            with pytest.raises(TenantNotFoundError):
                await assign_tenant_to_org(conn, tenant_id="不存在", org_id="muji")
        finally:
            await conn.close()

    asyncio.run(run())


def test_clearing_a_nonexistent_tenant_is_also_rejected():
    """org_id=None（移出组织）这条路径同样要校验 tenant_id 是否存在——
    移出一个不存在的租户跟分配给不存在的租户一样是静默失败，不能因为
    "只是清空"就放行。"""

    async def run():
        conn = await _conn()
        try:
            with pytest.raises(TenantNotFoundError):
                await assign_tenant_to_org(conn, tenant_id="不存在", org_id=None)
        finally:
            await conn.close()

    asyncio.run(run())


def test_org_id_can_be_cleared():
    """org_id 传 None 是「把这个租户移出组织」，不是报错——存量租户本来
    就不属于任何组织，这条路径必须走得通。"""

    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id=None)
            assert await list_tenants_in_org(conn, "muji") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_ensure_schema_is_idempotent_and_keeps_existing_org_id():
    """启动会重复跑 ensure。第二次把 org_id 清空的话，重启一次所有租户
    就掉出组织了——而没有任何人会注意到，直到打开看板发现全空。"""

    async def run():
        conn = await _conn()
        try:
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")
            await ensure_organizations_schema(conn)
            assert await list_tenants_in_org(conn, "muji") == ["muji-goods"]
        finally:
            await conn.close()

    asyncio.run(run())
