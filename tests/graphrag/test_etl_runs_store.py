from __future__ import annotations

import json

import aiosqlite
import pytest

from app.graphrag.etl_runs_store import (
    EtlRunAlreadyRunningError,
    EtlRunNotFoundError,
    create_etl_run,
    ensure_etl_runs_schema,
    get_etl_run,
    list_etl_runs,
    mark_etl_run_completed,
    mark_etl_run_failed,
)

pytestmark = pytest.mark.anyio


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_etl_runs_schema(conn)
    return conn


async def test_create_etl_run_then_get_returns_running_status():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")

    detail = await get_etl_run(conn, tenant_id="muji", run_id="r1")

    assert detail.status == "running"
    assert detail.finished_at is None
    assert detail.report is None
    assert detail.error is None


async def test_create_etl_run_rejects_second_running_run_for_same_tenant():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")

    with pytest.raises(EtlRunAlreadyRunningError):
        await create_etl_run(conn, run_id="r2", tenant_id="muji", started_at="2026-08-17T10:00:01")


async def test_create_etl_run_allows_concurrent_runs_for_different_tenants():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")

    await create_etl_run(conn, run_id="r2", tenant_id="acme", started_at="2026-08-17T10:00:01")  # 不应抛异常


async def test_create_etl_run_allows_new_run_after_previous_one_completed():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")
    await mark_etl_run_completed(
        conn, run_id="r1", finished_at="2026-08-17T10:05:00",
        report_json=json.dumps({"entities_written": 1}),
    )

    await create_etl_run(conn, run_id="r2", tenant_id="muji", started_at="2026-08-17T10:06:00")  # 不应抛异常


async def test_mark_etl_run_completed_populates_report():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")
    report_json = json.dumps({"entities_written": 3, "entities_skipped": 1}, ensure_ascii=False)

    await mark_etl_run_completed(conn, run_id="r1", finished_at="2026-08-17T10:05:00", report_json=report_json)

    detail = await get_etl_run(conn, tenant_id="muji", run_id="r1")
    assert detail.status == "completed"
    assert detail.finished_at == "2026-08-17T10:05:00"
    assert detail.report == {"entities_written": 3, "entities_skipped": 1}


async def test_mark_etl_run_failed_populates_error_not_report():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")

    await mark_etl_run_failed(conn, run_id="r1", finished_at="2026-08-17T10:01:00", error="租户 schema 未确认")

    detail = await get_etl_run(conn, tenant_id="muji", run_id="r1")
    assert detail.status == "failed"
    assert detail.error == "租户 schema 未确认"
    assert detail.report is None


async def test_get_etl_run_raises_when_not_found():
    conn = await _connect()

    with pytest.raises(EtlRunNotFoundError):
        await get_etl_run(conn, tenant_id="muji", run_id="nonexistent")


async def test_get_etl_run_raises_when_run_belongs_to_different_tenant():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")

    with pytest.raises(EtlRunNotFoundError):
        await get_etl_run(conn, tenant_id="acme", run_id="r1")


async def test_list_etl_runs_returns_only_that_tenant_ordered_newest_first():
    conn = await _connect()
    await create_etl_run(conn, run_id="r1", tenant_id="muji", started_at="2026-08-17T10:00:00")
    await mark_etl_run_completed(conn, run_id="r1", finished_at="2026-08-17T10:05:00", report_json="{}")
    await create_etl_run(conn, run_id="r2", tenant_id="muji", started_at="2026-08-17T11:00:00")
    await create_etl_run(conn, run_id="r3", tenant_id="acme", started_at="2026-08-17T09:00:00")

    result = await list_etl_runs(conn, "muji")

    assert [r.run_id for r in result] == ["r2", "r1"]
