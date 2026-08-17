# ETL 管理后台触发界面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给已有的 `app/graphrag/schema_etl.py`（CLI-only 写入引擎）加一个管理后台触发入口——上传 YAML 列映射配置 + CSV 数据文件，后台异步执行，前端轮询状态并展示跑批报告。

**Architecture:** 新建 `app/graphrag/etl_runs_store.py` 承担 `etl_runs` 表的读写（复用 `deps.get_review_conn` 单例连接，跟随现有 `terms`/`ontology` 表建表模式）；新建 `app/api/admin_schema_etl_routes.py`，用 FastAPI `BackgroundTasks` 异步跑 `run_schema_etl`（不引入新任务队列系统，模式与 `admin_document_routes.py::upload_document` 一致）；前端新增 `SchemaEtlPage.tsx`，上传+轮询+报告展示，模式与 `DocumentsPage.tsx` 一致。

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, React + TypeScript（前端沿用现有 `adminApi`/`useAdminAuth`/`useAdminTenant` 基础设施）。

**Spec:** `docs/superpowers/specs/2026-08-17-schema-etl-admin-ui-design.md`

## Global Constraints

- 同一租户同时只允许一次 `status='running'` 的 ETL 跑批——用数据库层的部分唯一索引（`CREATE UNIQUE INDEX ... WHERE status = 'running'`）兜底竞态窗口，不是纯应用层查询+判断——spec 第4节。
- 上传的 CSV 文件必须以其原始文件名落盘（消毒不安全字符，但不加 uuid 前缀）——`run_schema_etl` 靠 `data_dir / mapping.source_file` 按 YAML 里声明的确切文件名去读文件，加前缀会导致文件读不到。
- 上传文件跑完后不自动删除——spec 第7节。
- `report_json` 存完整报告（不截断 `skipped_rows`），截断只发生在前端展示层——spec 第3节、第6节。
- 页面/接口都不做定时调度、取消跑批、跨租户批量触发——spec 第10节范围外事项。

---

### Task 1: `etl_runs` 表读写模块

**Files:**
- Create: `app/graphrag/etl_runs_store.py`
- Test: `tests/graphrag/test_etl_runs_store.py`

**Interfaces:**
- Consumes: 无新依赖（只用 `aiosqlite`/`json`/`dataclasses`）。
- Produces：
  - `EtlRunSummary(run_id, tenant_id, status, started_at, finished_at)`
  - `EtlRunDetail(run_id, tenant_id, status, started_at, finished_at, report, error)`
  - `EtlRunAlreadyRunningError(Exception)`
  - `EtlRunNotFoundError(Exception)`
  - `async def ensure_etl_runs_schema(conn) -> None`
  - `async def create_etl_run(conn, *, run_id, tenant_id, started_at) -> None`
  - `async def mark_etl_run_completed(conn, *, run_id, finished_at, report_json) -> None`
  - `async def mark_etl_run_failed(conn, *, run_id, finished_at, error) -> None`
  - `async def get_etl_run(conn, *, tenant_id, run_id) -> EtlRunDetail`
  - `async def list_etl_runs(conn, tenant_id) -> list[EtlRunSummary]`

  这些供 Task 2（`deps.py` 建表接线）和 Task 3（路由）使用。

- [ ] **Step 1: 写失败的测试**

创建 `tests/graphrag/test_etl_runs_store.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_etl_runs_store.py -v`
Expected: FAIL（`app.graphrag.etl_runs_store` 模块不存在）。

- [ ] **Step 3: 实现**

创建 `app/graphrag/etl_runs_store.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/graphrag/test_etl_runs_store.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/etl_runs_store.py tests/graphrag/test_etl_runs_store.py
git commit -m "feat(graphrag): add etl_runs table store with per-tenant single-running-run guard"
```

---

### Task 2: 建表接线

**Files:**
- Modify: `app/api/deps.py`

**Interfaces:**
- Consumes: Task 1 的 `ensure_etl_runs_schema`。
- Produces: 无新公开接口——`get_review_conn()` 返回的连接现在保证 `etl_runs` 表已存在，供 Task 3 的路由直接使用。

- [ ] **Step 1: 实现**（这个改动只是给已有初始化流程加一行，没有独立可测的新行为，跳过 TDD 步骤，直接改）

在 `app/api/deps.py` 顶部 import 区新增：

```python
from app.graphrag.etl_runs_store import ensure_etl_runs_schema
```

在 `get_review_conn` 函数体内，`await ensure_ontology_schema(conn)` 那一行之后（`try` 块内、`except Exception:` 之前）追加：

```python
                    await ensure_etl_runs_schema(conn)
```

- [ ] **Step 2: 验证**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_deps.py -v`
Expected: 全部 PASS（确认没有破坏 `get_review_conn` 现有测试）。

- [ ] **Step 3: 提交**

```bash
git add app/api/deps.py
git commit -m "feat(api): create etl_runs table when the review connection initializes"
```

---

### Task 3: ETL 触发/查询/下载路由

**Files:**
- Create: `app/api/admin_schema_etl_routes.py`
- Modify: `app/main.py`
- Test: `tests/api/test_admin_schema_etl_routes.py`

**Interfaces:**
- Consumes: Task 1 的 `create_etl_run`/`get_etl_run`/`list_etl_runs`/`EtlRunAlreadyRunningError`/`EtlRunNotFoundError`；`app.graphrag.ontology_lifecycle.is_ontology_confirmed`；`app.graphrag.schema_etl.run_schema_etl`/`SchemaETLNotConfirmedError`；`app.graphrag.schema_etl_config.load_schema_etl_config`；`app.api.deps.get_review_conn`/`get_graph_client`/`get_upload_dir`/`require_admin_session`。
- Produces：5个路由（`GET /status`、`POST /runs`、`GET /runs`、`GET /runs/{run_id}`、`GET /runs/{run_id}/report.csv`），供 Task 4 前端调用；`router`（`APIRouter`）供 `app/main.py` 注册。

- [ ] **Step 1: 写失败的测试**

创建 `tests/api/test_admin_schema_etl_routes.py`（先读一遍 `tests/api/test_admin_ontology_routes.py` 或 `tests/api/test_admin_terms_routes.py`，把这个测试文件里 `client`/管理员登录 header/`graph_client` 打桩的具体 fixture 名字和写法对齐那个文件已有的方式，不要发明新的测试基础设施）：

```python
from __future__ import annotations

from pathlib import Path

import pytest

from app.graphrag.ontology_categories import create_product_line, create_term_type
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.terms_store import ensure_terms_schema

pytestmark = pytest.mark.anyio


async def _confirm_muji_schema(review_conn) -> None:
    await ensure_terms_schema(review_conn)
    await ensure_ontology_schema(review_conn)
    await create_term_type(review_conn, tenant_id="muji", value="Product")
    await create_product_line(review_conn, value="MUJI")
    await checkout_draft(review_conn, "muji")
    await confirm_ontology(review_conn, "muji")


async def test_status_returns_false_when_schema_not_confirmed(client):
    response = await client.get("/api/admin/unconfirmed_tenant/schema-etl/status")
    assert response.status_code == 200
    assert response.json() == {"ontology_confirmed": False}


async def test_status_returns_true_after_confirm(client, review_conn):
    await _confirm_muji_schema(review_conn)

    response = await client.get("/api/admin/muji/schema-etl/status")

    assert response.json() == {"ontology_confirmed": True}


async def test_start_run_rejects_when_schema_not_confirmed(client):
    files = {"config": ("config.yaml", b"tenant_id: unconfirmed_tenant\nentities: []\nrelations: []\n")}
    response = await client.post("/api/admin/unconfirmed_tenant/schema-etl/runs", files=files)
    assert response.status_code == 400


async def test_start_run_returns_run_id_and_second_concurrent_start_is_rejected(client, review_conn):
    await _confirm_muji_schema(review_conn)
    files = {"config": ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")}

    first = await client.post("/api/admin/muji/schema-etl/runs", files=files)
    assert first.status_code == 200
    run_id = first.json()["run_id"]
    assert run_id

    second = await client.post("/api/admin/muji/schema-etl/runs", files=files)
    assert second.status_code == 409


async def test_get_run_not_found_returns_404(client):
    response = await client.get("/api/admin/muji/schema-etl/runs/nonexistent")
    assert response.status_code == 404


async def test_list_runs_empty_for_new_tenant(client):
    response = await client.get("/api/admin/muji/schema-etl/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": []}


async def test_report_csv_returns_404_when_run_not_found(client):
    response = await client.get("/api/admin/muji/schema-etl/runs/nonexistent/report.csv")
    assert response.status_code == 404
```

（这些测试刻意避开真正等待后台任务跑完再断言报告内容——`BackgroundTasks` 在测试客户端下的执行时机和真实部署环境不完全一致，验证"跑批真的执行、报告真的写对"属于 Task 1 已经用直接函数调用测过的 `run_schema_etl`/`etl_runs_store` 职责，这里只测路由层自己的逻辑：状态码、409 并发拒绝、404 情况。如果这个测试文件已有的其它路由测试文件里有类似"不等后台任务、只测同步返回部分"的先例写法，跟随那个先例。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_admin_schema_etl_routes.py -v`
Expected: FAIL（模块/路由都不存在，404）。

- [ ] **Step 3: 实现**

创建 `app/api/admin_schema_etl_routes.py`：

```python
from __future__ import annotations

import csv
import io
import json
import logging
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api import deps
from app.graphrag.etl_runs_store import (
    EtlRunAlreadyRunningError,
    EtlRunNotFoundError,
    create_etl_run,
    get_etl_run,
    list_etl_runs,
    mark_etl_run_completed,
    mark_etl_run_failed,
)
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.schema_etl import SchemaETLNotConfirmedError, run_schema_etl
from app.graphrag.schema_etl_config import load_schema_etl_config

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/{tenant_id}/schema-etl", dependencies=[Depends(deps.require_admin_session)]
)

# 上传的 CSV 文件必须以原始文件名落盘——run_schema_etl 靠 data_dir /
# mapping.source_file 按 YAML 里声明的确切文件名去读文件，这里不能像
# admin_document_routes.py::_sanitize_filename 那样加 uuid 前缀（那是为了
# 避免同名文件互相覆盖，这里每次跑批都有自己独立的 run_id 目录，天然不会
# 撞名，加前缀反而会让文件名和 YAML 里的 source_file 对不上）。只消毒路径
# 分隔符等危险字符，防止用文件名逃出 run_dir。
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def _sanitize_data_filename(filename: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", filename) or "unnamed"


class StatusResponse(BaseModel):
    ontology_confirmed: bool


class StartRunResponse(BaseModel):
    run_id: str


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    started_at: str
    finished_at: str | None


class RunListResponse(BaseModel):
    runs: list[RunSummaryResponse]


class RunDetailResponse(BaseModel):
    run_id: str
    status: str
    started_at: str
    finished_at: str | None
    report: dict | None
    error: str | None


@router.get("/status", response_model=StatusResponse)
async def get_schema_etl_status(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> StatusResponse:
    return StatusResponse(ontology_confirmed=await is_ontology_confirmed(review_conn, tenant_id))


async def _run_schema_etl_job(
    *,
    conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    run_id: str,
    tenant_id: str,
    config_path: Path,
    data_dir: Path,
) -> None:
    """后台异步执行的实际跑批体——run_schema_etl 自己会再检查一次
    is_ontology_confirmed（本路由的 /runs 入口已经提前检查过一次，这里的
    检查是它自己的兜底，不是重复劳动），所以 SchemaETLNotConfirmedError
    在这里仍然是一个合法的、需要捕获的失败分支（例如页面检查通过后、
    真正跑批前，租户 schema 被别人改成未确认）。"""
    try:
        config = load_schema_etl_config(config_path)
        report = await run_schema_etl(conn=conn, graph_client=graph_client, config=config, data_dir=data_dir)
        await mark_etl_run_completed(
            conn,
            run_id=run_id,
            finished_at=datetime.now().isoformat(),
            report_json=json.dumps(asdict(report), ensure_ascii=False),
        )
    except Exception as exc:
        logger.exception("ETL 跑批 %r（租户 %r）执行失败", run_id, tenant_id)
        await mark_etl_run_failed(conn, run_id=run_id, finished_at=datetime.now().isoformat(), error=str(exc))


@router.post("/runs", response_model=StartRunResponse)
async def start_schema_etl_run(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    config: UploadFile,
    data_files: list[UploadFile] = [],
    upload_dir: Path = Depends(deps.get_upload_dir),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> StartRunResponse:
    if not await is_ontology_confirmed(review_conn, tenant_id):
        raise HTTPException(status_code=400, detail=f"租户 {tenant_id!r} 的本体 schema 还没有确认")

    run_id = uuid.uuid4().hex
    run_dir = upload_dir / "schema-etl" / tenant_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    config_path = run_dir / "config.yaml"
    config_path.write_bytes(await config.read())

    for data_file in data_files:
        if not data_file.filename:
            continue
        dest = run_dir / _sanitize_data_filename(data_file.filename)
        dest.write_bytes(await data_file.read())

    started_at = datetime.now().isoformat()
    try:
        await create_etl_run(review_conn, run_id=run_id, tenant_id=tenant_id, started_at=started_at)
    except EtlRunAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(
        _run_schema_etl_job,
        conn=review_conn,
        graph_client=graph_client,
        run_id=run_id,
        tenant_id=tenant_id,
        config_path=config_path,
        data_dir=run_dir,
    )
    return StartRunResponse(run_id=run_id)


@router.get("/runs", response_model=RunListResponse)
async def list_schema_etl_runs(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> RunListResponse:
    runs = await list_etl_runs(review_conn, tenant_id)
    return RunListResponse(
        runs=[
            RunSummaryResponse(
                run_id=r.run_id, status=r.status, started_at=r.started_at, finished_at=r.finished_at
            )
            for r in runs
        ]
    )


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def get_schema_etl_run(
    tenant_id: str, run_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> RunDetailResponse:
    try:
        detail = await get_etl_run(review_conn, tenant_id=tenant_id, run_id=run_id)
    except EtlRunNotFoundError:
        raise HTTPException(status_code=404, detail="跑批不存在")
    return RunDetailResponse(
        run_id=detail.run_id, status=detail.status, started_at=detail.started_at,
        finished_at=detail.finished_at, report=detail.report, error=detail.error,
    )


@router.get("/runs/{run_id}/report.csv")
async def download_schema_etl_report_csv(
    tenant_id: str, run_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> StreamingResponse:
    try:
        detail = await get_etl_run(review_conn, tenant_id=tenant_id, run_id=run_id)
    except EtlRunNotFoundError:
        raise HTTPException(status_code=404, detail="跑批不存在")
    if detail.report is None:
        raise HTTPException(status_code=404, detail="该跑批没有可下载的报告")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["label", "source_file", "row_number", "reason"])
    for row in detail.report.get("skipped_rows", []):
        writer.writerow([row["label"], row["source_file"], row["row_number"], row["reason"]])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_skipped_rows.csv"'},
    )
```

在 `app/main.py` 里，跟 `admin_terms_router` 同样的方式注册新路由——顶部 import 区加：

```python
from app.api.admin_schema_etl_routes import router as admin_schema_etl_router
```

在 `app.include_router(admin_terms_router)` 那一行之后追加：

```python
app.include_router(admin_schema_etl_router)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_admin_schema_etl_routes.py -v`
Expected: 全部 PASS。

Run: `.venv/Scripts/python.exe -u -m pytest -q tests/api/ -v`
Expected: 全部 PASS（确认没有破坏其它 admin 路由测试或 `app.main` 的启动）。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_schema_etl_routes.py app/main.py tests/api/test_admin_schema_etl_routes.py
git commit -m "feat(api): add schema ETL trigger/status/report routes"
```

---

### Task 4: 前端触发页面

**Files:**
- Create: `frontend/src/admin/SchemaEtlPage.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: Task 3 的5个路由；`frontend/src/admin/adminApi.ts` 的 `adminFetch`/`extractErrorDetail`；`frontend/src/admin/useAdminAuth.ts` 的 `useAdminAuth`；`frontend/src/admin/TenantContext.tsx` 的 `useAdminTenant`（三者都已存在，直接复用，不新建）。
- Produces: `SchemaEtlPage` 组件，挂载到 `/admin/schema-etl` 路由。

- [ ] **Step 1: 实现**

（前端没有已建立的单元测试基础设施覆盖这类页面组件——`DocumentsPage.tsx`/`TermsPage.tsx` 都没有对应的 `.test.tsx` 文件，本任务跟随这个既有惯例，不额外引入新的前端测试框架，用手动走查代替。）

创建 `frontend/src/admin/SchemaEtlPage.tsx`：

```tsx
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

interface RunSummary {
  run_id: string
  status: string
  started_at: string
  finished_at: string | null
}

interface SkippedRow {
  label: string
  source_file: string
  row_number: number
  reason: string
}

interface EtlRunReport {
  entities_written?: number
  entities_skipped?: number
  relations_written?: number
  relations_skipped?: number
  written_by_type?: Record<string, number>
  skipped_by_type?: Record<string, number>
  skipped_rows?: SkippedRow[]
  skipped_mappings?: { label: string; source_file: string; reason: string }[]
}

interface RunDetail {
  run_id: string
  status: string
  started_at: string
  finished_at: string | null
  report: EtlRunReport | null
  error: string | null
}

const SKIPPED_ROWS_PREVIEW_LIMIT = 50

export function SchemaEtlPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [confirmed, setConfirmed] = useState<boolean | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<RunDetail | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const hasRunningRef = useRef(false)
  const pollNowRef = useRef<() => Promise<void>>(async () => {})

  useEffect(() => {
    document.title = 'ETL 跑批 · 管理后台'
  }, [])

  const refreshStatus = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/status`,
      sessionToken,
    )
    const data = (await response.json()) as { ontology_confirmed: boolean }
    setConfirmed(data.ontology_confirmed)
  }, [sessionToken, tenantId])

  const refreshRuns = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs`,
      sessionToken,
    )
    const data = (await response.json()) as { runs: RunSummary[] }
    setRuns(data.runs)
    hasRunningRef.current = data.runs.some((r) => r.status === 'running')
  }, [sessionToken, tenantId])

  useEffect(() => {
    refreshStatus().catch((err) => console.error('查询 schema 确认状态失败', err))
  }, [refreshStatus])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      if (timer) clearTimeout(timer)
      try {
        await refreshRuns()
      } catch (err) {
        console.error('ETL 跑批列表刷新失败', err)
      }
      if (cancelled) return
      const interval = hasRunningRef.current ? 3000 : 15000
      timer = setTimeout(poll, interval)
    }
    pollNowRef.current = poll
    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [refreshRuns])

  useEffect(() => {
    if (!selectedRunId || !sessionToken) {
      setSelectedRun(null)
      return
    }
    let cancelled = false
    const load = async () => {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs/${encodeURIComponent(selectedRunId)}`,
        sessionToken,
      )
      if (cancelled) return
      const data = (await response.json()) as RunDetail
      setSelectedRun(data)
    }
    load().catch((err) => console.error('跑批详情加载失败', err))
    return () => {
      cancelled = true
    }
  }, [selectedRunId, sessionToken, tenantId])

  const handleUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!sessionToken) return
    const form = event.currentTarget
    const configInput = form.elements.namedItem('config') as HTMLInputElement
    const dataFilesInput = form.elements.namedItem('data_files') as HTMLInputElement
    const configFile = configInput.files?.[0]
    if (!configFile) return

    setUploading(true)
    setUploadError(null)
    try {
      const formData = new FormData()
      formData.append('config', configFile)
      for (const file of Array.from(dataFilesInput.files ?? [])) {
        formData.append('data_files', file)
      }
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs`,
        sessionToken,
        { method: 'POST', body: formData },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '启动失败'))
      }
      form.reset()
      await pollNowRef.current()
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '启动失败')
    } finally {
      setUploading(false)
    }
  }

  const downloadReportUrl = selectedRun
    ? `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs/${encodeURIComponent(selectedRun.run_id)}/report.csv`
    : null

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-xl font-semibold">ETL 跑批</h1>

      {confirmed === false && (
        <div className="rounded border border-amber-400 bg-amber-50 p-3 text-sm text-amber-800">
          该租户本体 schema 尚未确认，请先完成本体 schema 确认后再触发 ETL。
        </div>
      )}

      <form onSubmit={handleUpload} className="space-y-3 rounded border p-4" aria-disabled={confirmed !== true}>
        <div>
          <label className="block text-sm font-medium">列映射配置（YAML）</label>
          <input type="file" name="config" accept=".yaml,.yml" required disabled={confirmed !== true} />
        </div>
        <div>
          <label className="block text-sm font-medium">数据文件（CSV，可多选）</label>
          <input type="file" name="data_files" accept=".csv" multiple disabled={confirmed !== true} />
        </div>
        {uploadError && <p className="text-sm text-red-600">{uploadError}</p>}
        <button
          type="submit"
          disabled={uploading || confirmed !== true}
          className="rounded bg-ink px-4 py-2 text-sm text-white disabled:opacity-50"
        >
          {uploading ? '提交中…' : '开始运行'}
        </button>
      </form>

      <div className="space-y-2">
        <h2 className="text-lg font-medium">历史跑批</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left">
              <th>run_id</th>
              <th>状态</th>
              <th>开始时间</th>
              <th>结束时间</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.run_id}
                className="cursor-pointer hover:bg-gray-50"
                onClick={() => setSelectedRunId(run.run_id)}
              >
                <td>{run.run_id}</td>
                <td>{run.status}</td>
                <td>{run.started_at}</td>
                <td>{run.finished_at ?? '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selectedRun && (
        <div className="space-y-3 rounded border p-4">
          <h2 className="text-lg font-medium">跑批详情：{selectedRun.run_id}</h2>
          {selectedRun.status === 'failed' && (
            <p className="text-sm text-red-600">失败：{selectedRun.error}</p>
          )}
          {selectedRun.report && (
            <>
              <p className="text-sm">
                实体写入 {selectedRun.report.entities_written ?? 0} 条，跳过{' '}
                {selectedRun.report.entities_skipped ?? 0} 条；关系写入{' '}
                {selectedRun.report.relations_written ?? 0} 条，跳过{' '}
                {selectedRun.report.relations_skipped ?? 0} 条
              </p>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left">
                    <th>类型</th>
                    <th>写入</th>
                    <th>跳过</th>
                  </tr>
                </thead>
                <tbody>
                  {Array.from(
                    new Set([
                      ...Object.keys(selectedRun.report.written_by_type ?? {}),
                      ...Object.keys(selectedRun.report.skipped_by_type ?? {}),
                    ]),
                  ).map((label) => (
                    <tr key={label}>
                      <td>{label}</td>
                      <td>{selectedRun.report?.written_by_type?.[label] ?? 0}</td>
                      <td>{selectedRun.report?.skipped_by_type?.[label] ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {selectedRun.report.skipped_rows && selectedRun.report.skipped_rows.length > 0 && (
                <div>
                  <h3 className="font-medium">
                    跳过明细（预览前 {SKIPPED_ROWS_PREVIEW_LIMIT} 条，共{' '}
                    {selectedRun.report.skipped_rows.length} 条）
                  </h3>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left">
                        <th>文件</th>
                        <th>行号</th>
                        <th>原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedRun.report.skipped_rows.slice(0, SKIPPED_ROWS_PREVIEW_LIMIT).map((row, idx) => (
                        <tr key={idx}>
                          <td>{row.source_file}</td>
                          <td>{row.row_number}</td>
                          <td>{row.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {downloadReportUrl && (
                    <a href={downloadReportUrl} className="text-sm text-blue-600 underline">
                      下载完整报告 CSV
                    </a>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
```

在 `frontend/src/App.tsx` 顶部 import 区新增：

```tsx
import { SchemaEtlPage } from './admin/SchemaEtlPage'
```

在 `<Route path="terms" element={<TermsPage />} />` 之后追加：

```tsx
        <Route path="schema-etl" element={<SchemaEtlPage />} />
```

- [ ] **Step 2: 手动走查**

启动前端（`npm run dev`）+ 后端，登录管理后台，访问 `/admin/schema-etl`：
1. 确认未 confirm schema 的租户下，上传表单被禁用并显示提示。
2. 用一个已 confirm schema 的租户上传一份最小 YAML（`entities: []`, `relations: []`）+ 不带任何 CSV，确认能拿到 `run_id`、历史列表里出现一条记录、几秒后状态变成 `completed`。
3. 点开这条记录，确认总览数字、按类型统计表格正确渲染（空跑批应该都是0）。
4. 在同一个跑批还在 `running` 状态时再提交一次，确认前端展示出 409 对应的错误提示。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/admin/SchemaEtlPage.tsx frontend/src/App.tsx
git commit -m "feat(frontend): add schema ETL trigger page"
```

---

## 完成后

全部4个任务完成后，跑一次全量后端回归（`pytest -q`），确认没有破坏任何既有测试；用 `superpowers:subagent-driven-development` 的标准流程做一次全分支终审，重点检查：`_sanitize_data_filename` 的路径逃逸防护是否真的挡住了所有危险文件名（终审阶段可以补充对抗性测试）；`BackgroundTasks` 执行期间如果整个 FastAPI 进程重启，`running` 状态的记录会永久卡住（没有超时/僵尸任务清理机制）——这是本计划已知但刻意不处理的风险（`ingestion_jobs` 也有类似的现状，不是本次新引入的问题类别），终审阶段视情况决定是否需要补一条"启动时把所有 `running` 状态的历史记录标记为 `failed`"的兜底逻辑。
