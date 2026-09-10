import asyncio

import aiosqlite
import pytest

from app.graphrag.organizations_store import (
    InvalidOrganizationError,
    OrganizationAlreadyExistsError,
    OrganizationNotFoundError,
    assign_tenant_to_org,
    create_organization,
    ensure_organizations_schema,
    list_organizations,
    list_tenants_in_org,
    list_tenants_with_organization,
)
from app.graphrag.tenants_store import (
    TenantNotFoundError,
    create_tenant,
    create_tenants_table,
    set_tenant_status,
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


def test_list_organizations_does_not_depend_on_a_row_factory_someone_else_set():
    """2026-09-08 全分支评审 Important 3：list_organizations 和
    list_tenants_in_org 此前没有自设 row_factory，靠的是调用方（生产路径上
    是 get_admin_user）"碰巧"已经设过——跟 user_tenants_store.py /
    tenant_personas_store.py 已经修过两次的缺陷是同一种。这里的 fixture
    `_conn()` 自己设了 row_factory，会把这个缺陷遮住，所以必须用一个完全
    没设过 row_factory 的裸连接来验证。"""

    async def run():
        conn = await aiosqlite.connect(":memory:")
        try:
            await create_tenants_table(conn)
            await ensure_organizations_schema(conn)
            await create_organization(conn, org_id="muji", name="无印良品")
            rows = await list_organizations(conn)
            assert [r["org_id"] for r in rows] == ["muji"]
            assert [r["name"] for r in rows] == ["无印良品"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_list_tenants_in_org_does_not_depend_on_a_row_factory_someone_else_set():
    """跟上面那条同源：list_tenants_in_org 也按列名取值，两个查询里只修
    一个的话，"按列名取值的查询自己设 row_factory"这句在同一个文件里就有
    反例。"""

    async def run():
        conn = await aiosqlite.connect(":memory:")
        try:
            await create_tenants_table(conn)
            await ensure_organizations_schema(conn)
            await create_organization(conn, org_id="muji", name="无印良品")
            await create_tenant(conn, tenant_id="muji-goods", name="商品")
            await assign_tenant_to_org(conn, tenant_id="muji-goods", org_id="muji")
            assert await list_tenants_in_org(conn, "muji") == ["muji-goods"]
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


def test_list_tenants_with_organization_keeps_tenants_that_have_no_org():
    """没挂组织的租户必须照样列出来，org_id/org_name 为 None。

    没挂组织是**合法状态**（存量租户，见模块 docstring）。用内连接的话
    它们会从看板上整个消失——用户会以为租户被删了，然后去建一个同名的。
    """

    async def run():
        conn = await _conn()
        try:
            await create_tenant(conn, tenant_id="t-org", name="有组织")
            await create_tenant(conn, tenant_id="t-loner", name="没组织")
            await create_organization(conn, org_id="o1", name="组织一")
            await assign_tenant_to_org(conn, tenant_id="t-org", org_id="o1")

            rows = {r["tenant_id"]: r for r in await list_tenants_with_organization(conn)}

            assert set(rows) == {"t-org", "t-loner"}
            assert (rows["t-org"]["org_id"], rows["t-org"]["org_name"]) == ("o1", "组织一")
            assert (rows["t-loner"]["org_id"], rows["t-loner"]["org_name"]) == (None, None)
        finally:
            await conn.close()

    asyncio.run(run())


def test_list_tenants_with_organization_hides_disabled_tenants_by_default():
    """停用的租户默认不列。

    看板列出它的话，用户会切过去——然后发现读得到、写全是 404，那是最难查
    的一类状态。口径跟 tenants_store.list_tenants 一致：只有租户管理页会
    显式要 include_disabled。
    """

    async def run():
        conn = await _conn()
        try:
            await create_tenant(conn, tenant_id="t-live", name="启用中")
            await create_tenant(conn, tenant_id="t-dead", name="已停用")
            await set_tenant_status(conn, "t-dead", "disabled")

            default = await list_tenants_with_organization(conn)
            assert [r["tenant_id"] for r in default] == ["t-live"]

            # include_disabled=True 时两个都在——只断言默认那一档的话，
            # "把这个参数整个忽略掉"的实现在默认路径上照样绿。
            both = await list_tenants_with_organization(conn, include_disabled=True)
            assert [r["tenant_id"] for r in both] == ["t-dead", "t-live"]
        finally:
            await conn.close()

    asyncio.run(run())


async def test_a_blank_org_id_is_refused():
    """空 / 纯空白的 org_id 直接拒绝，不能只靠前端按钮禁用。

    空串是 SQLite TEXT PRIMARY KEY 的合法值，唯一性检查也过得去。建出来
    之后它会在每个租户的「所属组织」下拉框里跟 `<option value="">无</option>`
    撞车：用户选中这个组织的名字，实际发出的是「移出组织」——租户被静默地
    移出而不是移入，而这个组织永远挂不上任何租户。
    """
    conn = await _conn()
    try:
        for bad in ("", "   ", "\t"):
            with pytest.raises(InvalidOrganizationError):
                await create_organization(conn, org_id=bad, name="无印良品")
        # 反面：正常的要建得成，否则"一律拒绝"的实现也能让上面变绿。
        await create_organization(conn, org_id="muji", name="无印良品")
        assert [o["org_id"] for o in await list_organizations(conn)] == ["muji"]
    finally:
        await conn.close()


async def test_a_blank_name_is_refused():
    """名字也一样：一个没有名字的组织在列表上是一行空白，谁也认不出它是什么。"""
    conn = await _conn()
    try:
        with pytest.raises(InvalidOrganizationError):
            await create_organization(conn, org_id="muji", name="  ")
    finally:
        await conn.close()
