from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.tenants_store import (
    TenantAlreadyExistsError,
    TenantNotFoundError,
    create_tenant,
    create_tenants_table,
    ensure_tenants_schema,
    list_tenants,
    require_active_tenant,
    set_tenant_status,
)
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.graphrag.etl_runs_store import ensure_etl_runs_schema
from app.graphrag.review_queue import ensure_review_schema
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema, enqueue_ingestion_job
from app.ingestion.tracking import ensure_tracking_schema, record_ingested

pytestmark = pytest.mark.anyio


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_etl_runs_schema(conn)
    # 除了走 ensure_tenants_schema() 全量迁移回填路径的测试之外，其余测试
    # 直接调用 create_tenant/require_active_tenant/set_tenant_status 等
    # 函数，需要 tenants 表已经存在——这里补建一次表（不做迁移回填）。
    # 与 ensure_tenants_schema() 内部的 create_tenants_table() 调用幂等
    # 兼容（CREATE TABLE IF NOT EXISTS），两边都调用不会冲突或重复建表。
    await create_tenants_table(conn)
    return conn


async def _ingestion_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)
    return conn


async def test_ensure_tenants_schema_seeds_demo_when_no_historical_data():
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()

    await ensure_tenants_schema(review_conn, ingestion_conn)

    tenants = await list_tenants(review_conn)
    assert [t["tenant_id"] for t in tenants] == ["demo"]


async def test_ensure_tenants_schema_backfills_from_ingested_documents():
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    await record_ingested(
        ingestion_conn, tenant_id="acme", file_path="a.md", content_hash="h", chunk_count=1
    )

    await ensure_tenants_schema(review_conn, ingestion_conn)

    tenant_ids = {t["tenant_id"] for t in await list_tenants(review_conn)}
    assert "acme" in tenant_ids


async def test_ensure_tenants_schema_backfills_from_ingestion_jobs_only():
    """回归测试：一个租户只在 ingestion_jobs 里留过记录（比如所有上传都
    失败、只剩 dead job 行，从没有一条真正摄取成功过、terms/etl_runs/
    graph_review_queue 等表也从没写过它），_discover_historical_tenant_ids
    在修复前不扫 ingestion_jobs，会把这个租户漏掉，升级后它就没法再做
    任何管理端写操作。"""
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    await enqueue_ingestion_job(
        ingestion_conn, tenant_id="job-only-tenant", file_path="a.md",
        content_hash="h", action="ingest",
    )

    await ensure_tenants_schema(review_conn, ingestion_conn)

    tenant_ids = {t["tenant_id"] for t in await list_tenants(review_conn)}
    assert "job-only-tenant" in tenant_ids


async def test_ensure_tenants_schema_is_idempotent():
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()

    await ensure_tenants_schema(review_conn, ingestion_conn)
    await create_tenant(review_conn, tenant_id="acme", name="Acme")
    await ensure_tenants_schema(review_conn, ingestion_conn)

    tenants = await list_tenants(review_conn)
    assert [t["tenant_id"] for t in tenants].count("acme") == 1


async def test_create_tenant_rejects_duplicate():
    review_conn = await _review_conn()
    await create_tenant(review_conn, tenant_id="acme", name="Acme")

    with pytest.raises(TenantAlreadyExistsError):
        await create_tenant(review_conn, tenant_id="acme", name="Acme Again")


async def test_list_tenants_excludes_disabled_by_default():
    review_conn = await _review_conn()
    await create_tenant(review_conn, tenant_id="acme", name="Acme")
    await create_tenant(review_conn, tenant_id="globex", name="Globex")
    await set_tenant_status(review_conn, "globex", "disabled")

    active_only = await list_tenants(review_conn)
    assert [t["tenant_id"] for t in active_only] == ["acme"]

    all_tenants = await list_tenants(review_conn, include_disabled=True)
    assert {t["tenant_id"] for t in all_tenants} == {"acme", "globex"}


async def test_require_active_tenant_rejects_unknown_tenant():
    review_conn = await _review_conn()
    with pytest.raises(TenantNotFoundError):
        await require_active_tenant(review_conn, "nope")


async def test_require_active_tenant_rejects_disabled_tenant():
    review_conn = await _review_conn()
    await create_tenant(review_conn, tenant_id="acme", name="Acme")
    await set_tenant_status(review_conn, "acme", "disabled")

    with pytest.raises(TenantNotFoundError):
        await require_active_tenant(review_conn, "acme")


async def test_require_active_tenant_accepts_active_tenant():
    review_conn = await _review_conn()
    await create_tenant(review_conn, tenant_id="acme", name="Acme")
    await require_active_tenant(review_conn, "acme")  # 不抛异常即通过


async def test_set_tenant_status_rejects_unknown_tenant():
    review_conn = await _review_conn()
    with pytest.raises(TenantNotFoundError):
        await set_tenant_status(review_conn, "nope", "disabled")
