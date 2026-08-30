import aiosqlite
import pytest
from fastapi import HTTPException

from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.tenants_store import (
    create_tenant,
    create_tenants_table,
    set_tenant_status,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def conn():
    async with aiosqlite.connect(":memory:") as conn:
        await create_tenants_table(conn)
        yield conn


async def test_active_tenant_passes_through(conn):
    await create_tenant(conn, tenant_id="t1", name="租户一")

    await require_active_tenant_or_404(conn, "t1")


async def test_unknown_tenant_raises_404(conn):
    with pytest.raises(HTTPException) as excinfo:
        await require_active_tenant_or_404(conn, "从没注册过的租户")

    assert excinfo.value.status_code == 404
    # detail 逐字节钉死：这 24 个写路由此前各自手写这句翻译，改动前没有任何
    # 测试断言过它的内容，前端/调用方却可能已经在依赖这个文案。
    assert excinfo.value.detail == "租户不存在或未启用"


async def test_disabled_tenant_raises_404(conn):
    """已注册但被停用的租户同样是 404，不是 403。

    require_active_tenant 把"不存在"和"未启用"合并成同一个错误，写路由对外
    也只呈现一种结果——区分两者会把"这个租户 ID 是否存在"泄漏给未授权的
    调用方。这条用例把这个合并行为钉住，避免以后有人"顺手"把停用改成 403。
    """
    await create_tenant(conn, tenant_id="t1", name="租户一")
    await set_tenant_status(conn, "t1", "disabled")

    with pytest.raises(HTTPException) as excinfo:
        await require_active_tenant_or_404(conn, "t1")

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "租户不存在或未启用"
