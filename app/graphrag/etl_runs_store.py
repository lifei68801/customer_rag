from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etl_runs (
    run_id       TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    report_json  TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_etl_runs_tenant_status ON etl_runs (tenant_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_etl_runs_one_running_per_tenant
    ON etl_runs (tenant_id) WHERE status = 'running';
"""


@dataclass(frozen=True)
class EtlRunSummary:
    run_id: str
    tenant_id: str
    status: str
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class EtlRunDetail:
    run_id: str
    tenant_id: str
    status: str
    started_at: str
    finished_at: str | None
    report: dict | None
    error: str | None


class EtlRunAlreadyRunningError(Exception):
    """该租户已有一次 status='running' 的 ETL 跑批，拒绝再启动一次——见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第3.3节的
    串行执行假设，以及 docs/superpowers/specs/2026-08-17-schema-etl-admin-ui-
    design.md 第4节。"""


class EtlRunNotFoundError(Exception):
    """指定 run_id 不存在，或者存在但不属于调用方给定的 tenant_id——两种情况
    统一报同一个异常，不向调用方泄露"这个 run_id 属于别的租户"这个事实。"""


async def ensure_etl_runs_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def create_etl_run(
    conn: aiosqlite.Connection, *, run_id: str, tenant_id: str, started_at: str
) -> None:
    """插入一条 status='running' 的新跑批记录。idx_etl_runs_one_running_per_
    tenant 这条部分唯一索引保证同一租户同时只能有一条 running 记录——竞态
    窗口下第二个几乎同时的请求会在这里撞 IntegrityError，转换成
    EtlRunAlreadyRunningError，而不是让两次跑批都真的跑起来、破坏
    etl_stable_code_registry 的串行执行假设。"""
    try:
        await conn.execute(
            "INSERT INTO etl_runs (run_id, tenant_id, status, started_at) VALUES (?, ?, 'running', ?)",
            (run_id, tenant_id, started_at),
        )
        await conn.commit()
    except aiosqlite.IntegrityError:
        raise EtlRunAlreadyRunningError(f"租户 {tenant_id!r} 已有 ETL 任务在运行")


async def mark_etl_run_completed(
    conn: aiosqlite.Connection, *, run_id: str, finished_at: str, report_json: str
) -> None:
    await conn.execute(
        "UPDATE etl_runs SET status='completed', finished_at=?, report_json=? WHERE run_id=?",
        (finished_at, report_json, run_id),
    )
    await conn.commit()


async def mark_etl_run_failed(
    conn: aiosqlite.Connection, *, run_id: str, finished_at: str, error: str
) -> None:
    await conn.execute(
        "UPDATE etl_runs SET status='failed', finished_at=?, error=? WHERE run_id=?",
        (finished_at, error, run_id),
    )
    await conn.commit()


async def get_etl_run(conn: aiosqlite.Connection, *, tenant_id: str, run_id: str) -> EtlRunDetail:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT run_id, tenant_id, status, started_at, finished_at, report_json, error "
        "FROM etl_runs WHERE run_id = ? AND tenant_id = ?",
        (run_id, tenant_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise EtlRunNotFoundError(f"跑批 {run_id!r} 不存在")
    return EtlRunDetail(
        run_id=row["run_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        report=json.loads(row["report_json"]) if row["report_json"] else None,
        error=row["error"],
    )


async def list_etl_runs(conn: aiosqlite.Connection, tenant_id: str) -> list[EtlRunSummary]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT run_id, tenant_id, status, started_at, finished_at FROM etl_runs "
        "WHERE tenant_id = ? ORDER BY started_at DESC",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [
        EtlRunSummary(
            run_id=r["run_id"], tenant_id=r["tenant_id"], status=r["status"],
            started_at=r["started_at"], finished_at=r["finished_at"],
        )
        for r in rows
    ]
