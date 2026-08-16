from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.etl_stable_code_registry import (
    allocate_stable_code,
    ensure_stable_code_registry_schema,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)
    return conn


async def test_allocate_stable_code_first_time_assigns_00001():
    conn = await _conn()
    code = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    assert code == "00001"


async def test_allocate_stable_code_reuses_existing_code_for_same_raw_value():
    conn = await _conn()
    first = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    second = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    assert first == second == "00001"


async def test_allocate_stable_code_increments_within_same_scope():
    conn = await _conn()
    await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    code = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="草莓")
    assert code == "00002"


async def test_allocate_stable_code_scopes_independently():
    """不同 scope（如不同维度）下相同原始值应该各自分配独立编号，
    不共享同一套计数——见 spec 第 3.1 节 scope 的作用。"""
    conn = await _conn()
    code_a = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="黑色")
    code_b = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_008", raw_value="黑色")
    assert code_a == "00001"
    assert code_b == "00001"


async def test_allocate_stable_code_scopes_by_tenant():
    conn = await _conn()
    code_a = await allocate_stable_code(conn, tenant_id="tenant_a", scope="VariantValue:dim_007", raw_value="抹茶")
    code_b = await allocate_stable_code(conn, tenant_id="tenant_b", scope="VariantValue:dim_007", raw_value="抹茶")
    assert code_a == "00001"
    assert code_b == "00001"


async def test_ensure_stable_code_registry_schema_is_idempotent():
    conn = await _conn()
    await allocate_stable_code(conn, tenant_id="muji", scope="s", raw_value="v")
    await ensure_stable_code_registry_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    code = await allocate_stable_code(conn, tenant_id="muji", scope="s", raw_value="v")
    assert code == "00001"
