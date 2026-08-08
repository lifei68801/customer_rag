# 后台管理系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一个后台管理系统（登录鉴权 + 文档管理 + 知识图谱审核，含历史记录），通过前台导航栏入口进入，支持多租户切换。

**Architecture:** 后端在现有 FastAPI app 上新增 3 个 router（auth/documents/graph-reviews），复用已有的 `app/ingestion/`、`app/graphrag/review_queue.py` 底层函数，只补 HTTP 层和两处必要的 schema 迁移；前端引入 `react-router-dom`，新增一套 `/admin/*` 路由和页面组件，复用现有 `DESIGN.md` 里的 brutalism token，不引入新配色/字体体系。

**Tech Stack:** Python 3.12、FastAPI、aiosqlite、pytest（`asyncio_mode = "auto"`，测试函数直接写 `async def test_...`）；前端 React + Vite + TypeScript + Tailwind + 新增 `react-router-dom`。

## Global Constraints

- 所有新增 SQLite 表字段用 `ALTER TABLE ... ADD COLUMN` 迁移，不重建表，不破坏现有数据（现有环境已有历史数据）。
- 后端新代码遵循 `app/api/deps.py` 现有的 `Depends()` 依赖注入模式和 `app/config/settings.py` 的 `CUSTOMER_RAG_` 环境变量前缀约定，不引入新的配置读取方式。
- 前端新代码遵循 `frontend/DESIGN.md` 定义的色彩/字体/边框投影/按钮/触控目标/focus 态规则，不引入新 token、不引入图标库。
- 管理员 session token 存前端 `sessionStorage`，不用 `localStorage`。
- 单个上传文件大小上限 100MB，后端在接收阶段校验。
- 每个任务完成后运行对应验证命令（后端 `python -m pytest <test file> -v`，前端 `npm run typecheck`），通过后再 commit、再进入下一个任务。

---

### Task 1: `graph_review_queue` 加 `tenant_id`/`source` 列，修复 `approve_review` 漏传参数的 bug

**Files:**
- Create: `app/db_migrations.py`
- Modify: `app/graphrag/review_queue.py`
- Modify: `app/graphrag/normalization.py:113-159`（三处 `enqueue_for_review` 调用点）
- Modify: `tests/graphrag/test_review_queue.py`
- Modify: `tests/graphrag/test_normalization.py`（如果调用了 `enqueue_for_review`/`FakeGraphClient`，同步更新签名——先搜索确认）

**Interfaces:**
- Produces: `add_column_if_missing(conn, *, table: str, column: str, ddl: str) -> None`（`app/db_migrations.py`，Task 3 会复用）；`enqueue_for_review(..., source: str, tenant_id: str) -> int`；`list_pending_reviews(conn, *, tenant_id: str) -> list[dict]`；`approve_review(conn, *, review_id, subject_standard_name, object_standard_name, tenant_id, graph_client)`；`reject_review(conn, *, review_id, tenant_id, note=None)`。

现状：`graph_review_queue` 表没有 `tenant_id`/`source` 列，是全局共享表；`approve_review()` 调用 `graph_client.merge_relation()` 时漏传 `source`/`tenant_id`（`ReviewGraphClientProtocol.merge_relation` 要求这两个必填参数），现在只要调用 `approve_review()` 就会抛 `TypeError`——这是本任务顺带修的既有 bug，不是新引入的需求。

- [ ] **Step 1: 写 `app/db_migrations.py` 的失败测试**

```python
# tests/test_db_migrations.py
import aiosqlite

from app.db_migrations import add_column_if_missing


async def test_add_column_if_missing_adds_new_column():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    await conn.commit()

    await add_column_if_missing(conn, table="t", column="tenant_id", ddl="TEXT NOT NULL DEFAULT 'demo'")

    cursor = await conn.execute("PRAGMA table_info(t)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "tenant_id" in columns


async def test_add_column_if_missing_is_idempotent():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    await conn.commit()

    await add_column_if_missing(conn, table="t", column="tenant_id", ddl="TEXT NOT NULL DEFAULT 'demo'")
    # 第二次调用不应该报错（列已存在）
    await add_column_if_missing(conn, table="t", column="tenant_id", ddl="TEXT NOT NULL DEFAULT 'demo'")

    cursor = await conn.execute("PRAGMA table_info(t)")
    columns = [row[1] for row in await cursor.fetchall()]
    assert columns.count("tenant_id") == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_db_migrations.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.db_migrations'`）

- [ ] **Step 3: 实现 `app/db_migrations.py`**

```python
from __future__ import annotations

import aiosqlite


async def add_column_if_missing(
    conn: aiosqlite.Connection, *, table: str, column: str, ddl: str
) -> None:
    """幂等地给已存在的表加一列，可重复调用。

    SQLite 没有 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 语法，先用
    `PRAGMA table_info` 查现有列名避免重复 ALTER 报错，不依赖捕获异常
    判断"列已存在"（那样会把真正的 SQL 语法错误也一并吞掉）。
    """
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if column in existing_columns:
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await conn.commit()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_db_migrations.py -v`
Expected: PASS

- [ ] **Step 5: 给 `graph_review_queue` 表加迁移，修改 `review_queue.py` 全部函数签名**

把 `app/graphrag/review_queue.py` 的 `ensure_review_schema`/`enqueue_for_review`/`list_pending_reviews`/`_fetch_pending_row`/`approve_review`/`reject_review` 替换为：

```python
from __future__ import annotations

from typing import Any, Protocol

import aiosqlite

from app.db_migrations import add_column_if_missing

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_candidate TEXT NOT NULL,
    object_candidate TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    suggested_subject_standard_name TEXT,
    suggested_object_standard_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_review_queue_status
    ON graph_review_queue (status);
"""


class ReviewGraphClientProtocol(Protocol):
    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
    ) -> None: ...


class ReviewNotFoundError(Exception):
    """指定的 review_id 在该租户下不存在（包括存在于别的租户名下的情况——
    不做区分，统一按"不存在"处理，避免向调用方泄露"这个 id 属于别的租户"
    这个信息）。"""


class ReviewAlreadyResolvedError(Exception):
    """指定的 review_id 已经被批准或驳回过，不能重复处理。"""


async def ensure_review_schema(conn: aiosqlite.Connection) -> None:
    """幂等建表+迁移，可重复调用。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    # tenant_id 迁移历史数据默认回填 'demo'——项目里目前唯一真实产生过
    # 数据的租户就是 demo，见 docs/superpowers/specs/2026-08-08-admin-backend-design.md 第2节。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="tenant_id",
        ddl="TEXT NOT NULL DEFAULT 'demo'",
    )
    # source 记录候选关系抽取自哪个文档，approve_review 批准时要把它传给
    # graph_client.merge_relation()（写入图谱边的 source 属性，用于文档
    # 重新摄取时按 source 清理旧边）。历史数据没有这个信息，回填空字符串
    # （不是 NULL，避免下游拼接/比较时到处判空）。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="source",
        ddl="TEXT NOT NULL DEFAULT ''",
    )


async def enqueue_for_review(
    conn: aiosqlite.Connection,
    *,
    subject_candidate: str,
    object_candidate: str,
    relation_type: str,
    reason: str,
    source: str,
    tenant_id: str,
    suggested_subject_standard_name: str | None = None,
    suggested_object_standard_name: str | None = None,
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    source/tenant_id 是批准时写入图谱边所必需的信息，来自调用方
    normalize_and_write_relations() 本身已有的同名参数，这里改为必填，
    不给默认值——遗漏它们会让批准动作在写图谱这一步直接失败。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason, "
        "suggested_subject_standard_name, suggested_object_standard_name, "
        "source, tenant_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject_candidate,
            object_candidate,
            relation_type,
            reason,
            suggested_subject_standard_name,
            suggested_object_standard_name,
            source,
            tenant_id,
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_pending_reviews(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "source, created_at FROM graph_review_queue "
        "WHERE status = 'pending' AND tenant_id = ? ORDER BY review_id",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_resolved_reviews(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查已批准/已驳回的历史记录，按 resolved_at 倒序（最近处理的排前面）。

    status 为 None 时返回 approved+rejected 两种都算"已处理"的记录；
    传 'approved'/'rejected' 时只看其中一种。
    """
    conn.row_factory = aiosqlite.Row
    if status is None:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, created_at "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status IN ('approved', 'rejected') "
            "ORDER BY resolved_at DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT review_id, subject_candidate, object_candidate, relation_type, "
            "reason, status, resolved_at, resolved_note, source, created_at "
            "FROM graph_review_queue "
            "WHERE tenant_id = ? AND status = ? "
            "ORDER BY resolved_at DESC LIMIT ? OFFSET ?",
            (tenant_id, status, limit, offset),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _fetch_pending_row(
    conn: aiosqlite.Connection, review_id: int, *, tenant_id: str
) -> dict[str, Any]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM graph_review_queue WHERE review_id = ? AND tenant_id = ?",
        (review_id, tenant_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ReviewNotFoundError(f"待审核记录不存在: {review_id}")
    row_dict = dict(row)
    if row_dict["status"] != "pending":
        raise ReviewAlreadyResolvedError(
            f"待审核记录已处理过 (status={row_dict['status']}): {review_id}"
        )
    return row_dict


async def approve_review(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    subject_standard_name: str,
    object_standard_name: str,
    tenant_id: str,
    graph_client: ReviewGraphClientProtocol,
) -> None:
    """人工确认候选关系对应的标准名称后，写入图谱并把队列状态标记为已批准。"""
    row = await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    await graph_client.merge_relation(
        subject_standard_name=subject_standard_name,
        object_standard_name=object_standard_name,
        relation_type=row["relation_type"],
        source=row["source"],
        tenant_id=tenant_id,
    )
    await conn.execute(
        "UPDATE graph_review_queue SET status='approved', "
        "resolved_at=datetime('now'), resolved_note=? WHERE review_id=?",
        (f"{subject_standard_name} -> {object_standard_name}", review_id),
    )
    await conn.commit()


async def reject_review(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    tenant_id: str,
    note: str | None = None,
) -> None:
    """人工判定该候选是噪声/误抽取，标记驳回，不写入图谱。"""
    await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    await conn.execute(
        "UPDATE graph_review_queue SET status='rejected', "
        "resolved_at=datetime('now'), resolved_note=? WHERE review_id=?",
        (note, review_id),
    )
    await conn.commit()
```

- [ ] **Step 6: 更新 `app/graphrag/normalization.py` 三处 `enqueue_for_review` 调用点**

在 `normalize_and_write_relations` 里，三处 `enqueue_for_review(review_conn, ...)` 调用（对应 `fuzzy_match_needs_confirmation`/`subject_unresolved`or`object_unresolved`/`invalid_relation_type` 三种 reason）都加上 `source=source, tenant_id=tenant_id`（函数参数里本来就有这两个值，只是之前没往下传）：

```python
                if review_conn is not None:
                    await enqueue_for_review(
                        review_conn,
                        subject_candidate=relation["subject"],
                        object_candidate=relation["object"],
                        relation_type=relation["relation_type"],
                        reason="fuzzy_match_needs_confirmation",
                        source=source,
                        tenant_id=tenant_id,
                        suggested_subject_standard_name=suggested_subject,
                        suggested_object_standard_name=suggested_object,
                    )
```

（另外两处同样加 `source=source, tenant_id=tenant_id`，其余参数不变。）

- [ ] **Step 7: 更新 `tests/graphrag/test_review_queue.py`**

把整个文件替换为（`_connect` 不变；`FakeGraphClient.merge_relation` 补上 `source`/`tenant_id` 形参；所有 `enqueue_for_review`/`list_pending_reviews`/`approve_review`/`reject_review` 调用补上 `source`/`tenant_id`；新增跨租户隔离和历史记录的用例）：

```python
import aiosqlite
import pytest

from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    approve_review,
    enqueue_for_review,
    ensure_review_schema,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    return conn


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type, source, tenant_id
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
            }
        )


async def test_enqueue_then_list_pending_returns_the_candidate():
    conn = await _connect()

    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="faq.md",
        tenant_id="t1",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["review_id"] == review_id
    assert pending[0]["subject_candidate"] == "网关超时示例2.0"
    assert pending[0]["reason"] == "subject_unresolved"
    assert pending[0]["source"] == "faq.md"


async def test_list_pending_reviews_does_not_leak_another_tenants_rows():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    assert await list_pending_reviews(conn, tenant_id="t2") == []


async def test_approve_review_writes_relation_with_source_and_tenant_and_removes_from_pending():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="faq.md",
        tenant_id="t1",
    )

    await approve_review(
        conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
    )

    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": "faq.md",
            "tenant_id": "t1",
        }
    ]
    assert await list_pending_reviews(conn, tenant_id="t1") == []


async def test_approve_review_from_wrong_tenant_raises_not_found():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    with pytest.raises(ReviewNotFoundError):
        await approve_review(
            conn, review_id=review_id, subject_standard_name="x",
            object_standard_name="y", tenant_id="t2", graph_client=graph_client,
        )
    assert graph_client.written == []


async def test_reject_review_removes_from_pending_without_writing():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="不存在的东西",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    await reject_review(conn, review_id=review_id, tenant_id="t1", note="确认是噪声，非真实实体")

    assert await list_pending_reviews(conn, tenant_id="t1") == []
    assert graph_client.written == []


async def test_approve_unknown_review_id_raises():
    conn = await _connect()
    graph_client = FakeGraphClient()

    with pytest.raises(ReviewNotFoundError):
        await approve_review(
            conn,
            review_id=999,
            subject_standard_name="a",
            object_standard_name="b",
            tenant_id="t1",
            graph_client=graph_client,
        )


async def test_approve_already_resolved_review_raises():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn, subject_candidate="x", object_candidate="y", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await reject_review(conn, review_id=review_id, tenant_id="t1")

    with pytest.raises(ReviewAlreadyResolvedError):
        await approve_review(
            conn,
            review_id=review_id,
            subject_standard_name="a",
            object_standard_name="b",
            tenant_id="t1",
            graph_client=graph_client,
        )


async def test_enqueue_with_suggested_names_is_returned_by_list_pending():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="网关超时了",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="fuzzy_match_needs_confirmation",
        source="s.md",
        tenant_id="t1",
        suggested_subject_standard_name="错误码E502",
        suggested_object_standard_name=None,
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["suggested_subject_standard_name"] == "错误码E502"
    assert pending[0]["suggested_object_standard_name"] is None


async def test_enqueue_without_suggested_names_defaults_to_null():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="不存在的东西",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["suggested_subject_standard_name"] is None
    assert pending[0]["suggested_object_standard_name"] is None


async def test_list_resolved_reviews_returns_approved_and_rejected_ordered_by_resolved_at():
    conn = await _connect()
    graph_client = FakeGraphClient()
    approved_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    rejected_id = await enqueue_for_review(
        conn, subject_candidate="c", object_candidate="d", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await approve_review(
        conn, review_id=approved_id, subject_standard_name="A", object_standard_name="B",
        tenant_id="t1", graph_client=graph_client,
    )
    await reject_review(conn, review_id=rejected_id, tenant_id="t1", note="噪声")

    resolved = await list_resolved_reviews(conn, tenant_id="t1")
    assert {r["review_id"] for r in resolved} == {approved_id, rejected_id}

    only_approved = await list_resolved_reviews(conn, tenant_id="t1", status="approved")
    assert [r["review_id"] for r in only_approved] == [approved_id]


async def test_list_resolved_reviews_does_not_include_pending():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    assert await list_resolved_reviews(conn, tenant_id="t1") == []
```

- [ ] **Step 8: 确认还有 `app/graphrag/review_cli.py` 引用了旧签名**

已确认 `app/graphrag/review_cli.py` 的 `cmd_list`/`cmd_approve`/`cmd_reject` 三个函数调用了 `list_pending_reviews`/`approve_review`/`reject_review`，本步骤不改这个文件（改动内容见 Task 4b，跟在 Task 4 后面执行，因为 Task 4b 里 `cmd_approve` 需要的 `graph_client` 走的是真实 `Neo4jGraphClient`，而这个依赖链和 Task 4 的 `deps.py` 改动更近，放在一起改动更聚焦）。这里只需确认：**不要现在就跑 `tests/graphrag/test_review_cli.py`**，它在 Task 4b 完成前会因为签名不匹配而失败，这是预期状态。

- [ ] **Step 9: 运行本任务改动范围内的测试确认通过**

Run: `python -m pytest tests/graphrag/test_review_queue.py tests/test_db_migrations.py -v`
Expected: 全部 PASS（`tests/graphrag/test_review_cli.py` 此时会失败，属预期状态，Task 4b 会修复它，不在本任务范围内运行）

- [ ] **Step 10: Commit**

```bash
git add app/db_migrations.py app/graphrag/review_queue.py app/graphrag/normalization.py tests/test_db_migrations.py tests/graphrag/test_review_queue.py
git commit -m "fix: add tenant_id/source to graph_review_queue, fix missing merge_relation args"
```

---

### Task 2: `ingestion_jobs` 加 `build_graph` 列，`process_pending_jobs` 支持逐任务开关图谱构建

**Files:**
- Modify: `app/ingestion/ingestion_queue.py`
- Modify: `tests/ingestion/test_ingestion_queue.py`

**Interfaces:**
- Consumes: `add_column_if_missing`（Task 1 产出）
- Produces: `enqueue_ingestion_job(conn, *, tenant_id, file_path, content_hash, action, build_graph: bool = False) -> str`；`process_pending_jobs(...)` 内部按 `job["build_graph"]` 决定是否传图谱资源，对外签名不变。

- [ ] **Step 1: 写失败测试——逐任务开关图谱构建**

在 `tests/ingestion/test_ingestion_queue.py` 追加：

```python
async def test_process_pending_jobs_skips_graph_extraction_when_build_graph_is_false(
    tmp_path,
):
    file_path = tmp_path / "a.md"
    file_path.write_text("## 主题\n内容。\n", encoding="utf-8")
    conn = await _connect()
    content_hash = compute_file_hash(file_path)
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(file_path), content_hash=content_hash,
        action="ingest", build_graph=False,
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    graph_calls = {"count": 0}

    class FailingGraphClient:
        async def merge_relation(self, **kwargs):
            graph_calls["count"] += 1

    await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        graph_client=FailingGraphClient(),
        graph_llm_registry="not-none-marker",
        graph_llm_provider_name="fake-embedding",
        graph_terms=[],
    )

    # build_graph=False 时即使外部传了图谱资源，这条任务也不该触发图谱抽取
    assert graph_calls["count"] == 0


async def test_enqueue_records_build_graph_flag():
    conn = await _connect()

    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest",
        build_graph=True,
    )

    pending = await list_pending_jobs(conn)
    assert pending[0]["job_id"] == job_id
    assert pending[0]["build_graph"] == 1


async def test_enqueue_different_build_graph_flag_creates_separate_job():
    conn = await _connect()

    job_id_1 = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest",
        build_graph=False,
    )
    job_id_2 = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest",
        build_graph=True,
    )

    assert job_id_1 != job_id_2
    assert len(await list_pending_jobs(conn)) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/ingestion/test_ingestion_queue.py -v`
Expected: FAIL（`build_graph` 参数不存在 / 列不存在）

- [ ] **Step 3: 实现改动**

`app/ingestion/ingestion_queue.py` 顶部 import 加 `from app.db_migrations import add_column_if_missing`。

`ensure_ingestion_queue_schema` 改为：

```python
async def ensure_ingestion_queue_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    # build_graph 支持逐任务决定是否触发图谱构建，历史任务默认 0（不建图，
    # 与迁移前的行为一致——迁移前所有任务的图谱资源都是调用方整批统一决定的）。
    await add_column_if_missing(
        conn, table="ingestion_jobs", column="build_graph",
        ddl="INTEGER NOT NULL DEFAULT 0",
    )
```

`_compute_dedupe_key` 加 `build_graph` 参与哈希（同一文件用不同 `build_graph` 值上传，应该是两条独立任务，不能互相覆盖去重）：

```python
def _compute_dedupe_key(
    *, tenant_id: str, file_path: str, content_hash: str, action: str, build_graph: bool
) -> str:
    raw = f"{tenant_id}:{file_path}:{content_hash}:{action}:{build_graph}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

`enqueue_ingestion_job` 签名和插入语句：

```python
async def enqueue_ingestion_job(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    file_path: str,
    content_hash: str,
    action: str,
    build_graph: bool = False,
) -> str:
    """入队一个摄取任务（action 为 'ingest' 或 'delete'），幂等：同一个
    (tenant_id, file_path, content_hash, action, build_graph) 组合重复
    入队只创建一条记录。
    """
    dedupe_key = _compute_dedupe_key(
        tenant_id=tenant_id, file_path=file_path, content_hash=content_hash,
        action=action, build_graph=build_graph,
    )
    job_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO ingestion_jobs "
        "(job_id, dedupe_key, tenant_id, file_path, content_hash, action, build_graph) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dedupe_key) DO NOTHING",
        (job_id, dedupe_key, tenant_id, file_path, content_hash, action, int(build_graph)),
    )
    await conn.commit()
    cursor = await conn.execute(
        "SELECT job_id FROM ingestion_jobs WHERE dedupe_key = ?", (dedupe_key,)
    )
    row = await cursor.fetchone()
    return row[0]
```

`process_pending_jobs` 循环体内，把原来无条件传图谱参数的那段改成按 `job["build_graph"]` 判断：

```python
            else:
                chunks = _parse_file(Path(file_path), ocr=ocr)
                use_graph = bool(job["build_graph"])
                chunk_count = await _ingest_chunks(
                    chunks,
                    Path(file_path),
                    embedding_registry=embedding_registry,
                    embedding_provider_name=embedding_provider_name,
                    vector_store=vector_store,
                    tenant_id=tenant_id,
                    graph_llm_registry=graph_llm_registry if use_graph else None,
                    graph_llm_provider_name=graph_llm_provider_name if use_graph else None,
                    graph_terms=graph_terms if use_graph else None,
                    graph_client=graph_client if use_graph else None,
                    graph_review_conn=graph_review_conn if use_graph else None,
                )
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/ingestion/test_ingestion_queue.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/ingestion/ingestion_queue.py tests/ingestion/test_ingestion_queue.py
git commit -m "feat: add per-job build_graph flag to ingestion queue"
```

---

### Task 3: 管理员认证 —— 配置项 + session 存取模块

**Files:**
- Modify: `app/config/settings.py`
- Create: `app/api/admin_session.py`
- Create: `tests/api/test_admin_session.py`

**Interfaces:**
- Consumes: `Settings.admin_token: str | None`（本任务新增）
- Produces: `create_session(*, ttl_seconds: int = 28800) -> str`；`verify_session(token: str) -> bool`；`AdminSessionStore` 类（进程内单例，Task 4 会通过 `deps.py` 包装成 FastAPI 依赖）

- [ ] **Step 1: `Settings` 加管理员 token 配置**

在 `app/config/settings.py` 的 `gateway_shared_secret` 字段后面加：

```python
    # 后台管理系统的管理员 token（登录凭证），未配置时 /admin/auth/login
    # 直接拒绝所有登录请求（而不是静默放行）——这和 gateway_shared_secret
    # 的"未配置=本地兜底"降级路径不同，后台管理能直接写库（上传文档、
    # 批准/驳回知识图谱关系），没有"无鉴权也能跑"的必要性。
    admin_token: str | None = None
```

- [ ] **Step 2: 写失败测试**

```python
# tests/api/test_admin_session.py
import time

from app.api.admin_session import AdminSessionStore


def test_create_session_returns_a_token_that_verifies_true():
    store = AdminSessionStore()

    token = store.create_session()

    assert store.verify_session(token) is True


def test_verify_unknown_token_returns_false():
    store = AdminSessionStore()

    assert store.verify_session("not-a-real-token") is False


def test_verify_expired_token_returns_false():
    store = AdminSessionStore()
    token = store.create_session(ttl_seconds=-1)  # 立即过期

    assert store.verify_session(token) is False


def test_revoke_session_invalidates_the_token():
    store = AdminSessionStore()
    token = store.create_session()

    store.revoke_session(token)

    assert store.verify_session(token) is False
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/api/test_admin_session.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 实现 `app/api/admin_session.py`**

```python
from __future__ import annotations

import secrets
import time


class AdminSessionStore:
    """进程内管理员 session 存取，token -> 过期时间戳（epoch seconds）。

    不做持久化——管理员 session 本来就设计成短期有效（默认 8 小时），
    进程重启导致所有人重新登录是可接受的代价，换来不用额外引入
    JWT 签名/SQLite 表这些复杂度。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}

    def create_session(self, *, ttl_seconds: int = 28800) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + ttl_seconds
        return token

    def verify_session(self, token: str) -> bool:
        expires_at = self._sessions.get(token)
        if expires_at is None:
            return False
        if time.time() >= expires_at:
            del self._sessions[token]
            return False
        return True

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/api/test_admin_session.py -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add app/config/settings.py app/api/admin_session.py tests/api/test_admin_session.py
git commit -m "feat: add admin session store for backend admin auth"
```

---

### Task 4: 管理员登录/鉴权路由 + FastAPI 依赖

**Files:**
- Modify: `app/api/deps.py`
- Create: `app/api/admin_auth_routes.py`
- Modify: `app/main.py`
- Create: `tests/api/test_admin_auth_routes.py`

**Interfaces:**
- Consumes: `AdminSessionStore`（Task 3）
- Produces: `deps.get_admin_session_store() -> AdminSessionStore`（进程内单例，模式同 `get_memory_conn`）；`deps.require_admin_session(...)` FastAPI 依赖，校验 `Authorization: Bearer <token>`，失败抛 401；`POST /admin/auth/login` 路由。

- [ ] **Step 1: `deps.py` 加单例依赖**

在 `app/api/deps.py` 顶部 import 加：

```python
from fastapi import Depends, Header, HTTPException
from app.api.admin_session import AdminSessionStore
```

（`Depends`/`Header`/`HTTPException` 已经 import 过，只需确认存在，不要重复 import。）

在文件末尾追加：

```python
_admin_session_store_cache: AdminSessionStore | None = None


def get_admin_session_store() -> AdminSessionStore:
    """进程内单例：所有管理员 session 共用同一份内存存储。"""
    global _admin_session_store_cache
    if _admin_session_store_cache is None:
        _admin_session_store_cache = AdminSessionStore()
    return _admin_session_store_cache


async def require_admin_session(
    authorization: str | None = Header(default=None),
    session_store: AdminSessionStore = Depends(get_admin_session_store),
) -> None:
    """校验 Authorization: Bearer <token> 是否是有效的管理员 session。

    所有 /admin/* 路由（登录接口本身除外）都应该依赖这个函数。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少管理员登录凭证")
    token = authorization.removeprefix("Bearer ")
    if not session_store.verify_session(token):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
```

同时把 `__all__` 列表加上 `"get_admin_session_store"` 和 `"require_admin_session"`。

- [ ] **Step 2: 写失败测试**

```python
# tests/api/test_admin_auth_routes.py
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.main import app


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        admin_token="correct-token",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_login_with_correct_token_returns_session_token():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.post("/admin/auth/login", json={"admin_token": "correct-token"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "session_token" in response.json()


def test_login_with_wrong_token_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.post("/admin/auth/login", json={"admin_token": "wrong"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_login_when_admin_token_not_configured_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings(admin_token=None)
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.post("/admin/auth/login", json={"admin_token": "anything"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_admin_protected_route_rejects_missing_token():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.get("/admin/auth/whoami")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_admin_protected_route_accepts_valid_session():
    session_store = AdminSessionStore()
    token = session_store.create_session()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    try:
        client = TestClient(app)
        response = client.get(
            "/admin/auth/whoami", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/api/test_admin_auth_routes.py -v`
Expected: FAIL（路由不存在，404）

- [ ] **Step 4: 实现 `app/api/admin_auth_routes.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings

router = APIRouter(prefix="/admin/auth")


class LoginRequest(BaseModel):
    admin_token: str


class LoginResponse(BaseModel):
    session_token: str


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    settings: Settings = Depends(deps.get_settings),
    session_store: AdminSessionStore = Depends(deps.get_admin_session_store),
) -> LoginResponse:
    if not settings.admin_token or payload.admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="管理员 token 不正确")
    session_token = session_store.create_session()
    return LoginResponse(session_token=session_token)


@router.get("/whoami", dependencies=[Depends(deps.require_admin_session)])
async def whoami() -> dict[str, bool]:
    return {"authenticated": True}
```

- [ ] **Step 5: 挂载到 `app/main.py`**

```python
from app.api.admin_auth_routes import router as admin_auth_router
```

`app.include_router(voice_router)` 后面加：

```python
app.include_router(admin_auth_router)
```

- [ ] **Step 6: 运行确认通过**

Run: `python -m pytest tests/api/test_admin_auth_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add app/api/deps.py app/api/admin_auth_routes.py app/main.py tests/api/test_admin_auth_routes.py
git commit -m "feat: add admin login endpoint and session-based auth dependency"
```

---

### Task 4b: 给知识图谱审核 CLI 补 `--tenant-id`（修复 Task 1 签名改动带来的破坏）

**Files:**
- Modify: `app/graphrag/review_cli.py`
- Modify: `tests/graphrag/test_review_cli.py`

**Interfaces:**
- Consumes: `list_pending_reviews`/`approve_review`/`reject_review`（Task 1 新签名，都要求 `tenant_id`）

- [ ] **Step 1: 更新 `tests/graphrag/test_review_cli.py`（先改测试，锁定新行为）**

把整个文件替换为：

```python
import aiosqlite

from app.graphrag.review_cli import cmd_approve, cmd_list, cmd_reject
from app.graphrag.review_queue import enqueue_for_review, ensure_review_schema, list_pending_reviews


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    return conn


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type, source, tenant_id
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
            }
        )


async def test_cmd_list_returns_pending_rows():
    conn = await _connect()
    await enqueue_for_review(
        conn,
        subject_candidate="a",
        object_candidate="b",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    pending = await cmd_list(review_conn=conn, tenant_id="t1")

    assert len(pending) == 1
    assert pending[0]["subject_candidate"] == "a"


async def test_cmd_list_does_not_leak_another_tenant():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    assert await cmd_list(review_conn=conn, tenant_id="t2") == []


async def test_cmd_approve_writes_relation_via_graph_client():
    conn = await _connect()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块示例",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="faq.md",
        tenant_id="t1",
    )
    graph_client = FakeGraphClient()

    await cmd_approve(
        review_conn=conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
    )

    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": "faq.md",
            "tenant_id": "t1",
        }
    ]
    assert await list_pending_reviews(conn, tenant_id="t1") == []


async def test_cmd_reject_removes_from_pending():
    conn = await _connect()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="噪声实体",
        object_candidate="另一个噪声",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    await cmd_reject(review_conn=conn, review_id=review_id, tenant_id="t1", note="确认是噪声")

    assert await list_pending_reviews(conn, tenant_id="t1") == []


async def test_cmd_list_prints_suggested_standard_names_when_present(capsys):
    conn = await _connect()
    await enqueue_for_review(
        conn,
        subject_candidate="网关超时了",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="fuzzy_match_needs_confirmation",
        source="s.md",
        tenant_id="t1",
        suggested_subject_standard_name="错误码E502",
        suggested_object_standard_name=None,
    )

    await cmd_list(review_conn=conn, tenant_id="t1")

    captured = capsys.readouterr()
    assert "建议" in captured.out
    assert "subject→错误码E502" in captured.out


async def test_cmd_list_does_not_print_suggestion_section_when_absent(capsys):
    conn = await _connect()
    await enqueue_for_review(
        conn,
        subject_candidate="a",
        object_candidate="b",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    await cmd_list(review_conn=conn, tenant_id="t1")

    captured = capsys.readouterr()
    assert "建议" not in captured.out
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/graphrag/test_review_cli.py -v`
Expected: FAIL（`cmd_list()` 等函数还不接受 `tenant_id` 参数）

- [ ] **Step 3: 更新 `app/graphrag/review_cli.py`**

把 `cmd_list`/`cmd_approve`/`cmd_reject`/`_parse_args`/`_main` 替换为：

```python
async def cmd_list(*, review_conn: aiosqlite.Connection, tenant_id: str) -> list[dict[str, Any]]:
    """列出所有待审核的候选关系，打印到终端并返回，便于测试断言。"""
    pending = await list_pending_reviews(review_conn, tenant_id=tenant_id)
    if not pending:
        print("没有待审核的候选关系。")
    for row in pending:
        suggestion_parts = []
        if row.get("suggested_subject_standard_name"):
            suggestion_parts.append(f"subject→{row['suggested_subject_standard_name']}")
        if row.get("suggested_object_standard_name"):
            suggestion_parts.append(f"object→{row['suggested_object_standard_name']}")
        suggestion_text = (
            f" (建议: {', '.join(suggestion_parts)})" if suggestion_parts else ""
        )
        print(
            f"[{row['review_id']}] {row['subject_candidate']} "
            f"--{row['relation_type']}--> {row['object_candidate']} "
            f"(原因: {row['reason']}, 时间: {row['created_at']}){suggestion_text}"
        )
    return pending


async def cmd_approve(
    *,
    review_conn: aiosqlite.Connection,
    review_id: int,
    subject_standard_name: str,
    object_standard_name: str,
    tenant_id: str,
    graph_client: ReviewGraphClientProtocol,
) -> None:
    await approve_review(
        review_conn,
        review_id=review_id,
        subject_standard_name=subject_standard_name,
        object_standard_name=object_standard_name,
        tenant_id=tenant_id,
        graph_client=graph_client,
    )
    print(
        f"已批准 review_id={review_id}，写入图谱："
        f"{subject_standard_name} -> {object_standard_name}"
    )


async def cmd_reject(
    *,
    review_conn: aiosqlite.Connection,
    review_id: int,
    tenant_id: str,
    note: str | None = None,
) -> None:
    await reject_review(review_conn, review_id=review_id, tenant_id=tenant_id, note=note)
    print(f"已驳回 review_id={review_id}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GraphRAG 人工待审核队列管理")
    parser.add_argument("--tenant-id", required=True, help="要操作的租户 ID")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="列出所有待审核的候选关系")

    approve_parser = subparsers.add_parser("approve", help="批准一条候选关系并写入图谱")
    approve_parser.add_argument("review_id", type=int)
    approve_parser.add_argument(
        "--subject", required=True, help="人工确认的 subject 标准名（须已在术语表中）"
    )
    approve_parser.add_argument(
        "--object", required=True, help="人工确认的 object 标准名（须已在术语表中）"
    )

    reject_parser = subparsers.add_parser("reject", help="驳回一条候选关系（判定为噪声/误抽取）")
    reject_parser.add_argument("review_id", type=int)
    reject_parser.add_argument("--note", default=None, help="驳回原因备注")

    return parser.parse_args()


async def _main() -> None:
    """CLI 入口。

    用法（--tenant-id 是顶层参数，必须写在子命令前面）：
      python -m app.graphrag.review_cli --tenant-id demo list
      python -m app.graphrag.review_cli --tenant-id demo approve 3 --subject 示例错误码E502 --object 示例登录模块
      python -m app.graphrag.review_cli --tenant-id demo reject 3 --note 确认是噪声
    """
    args = _parse_args()
    settings = Settings()
    review_conn = await build_review_conn_from_settings(settings)

    if args.command == "list":
        await cmd_list(review_conn=review_conn, tenant_id=args.tenant_id)
    elif args.command == "approve":
        graph_client = build_graph_client_from_settings(settings)
        await cmd_approve(
            review_conn=review_conn,
            review_id=args.review_id,
            subject_standard_name=args.subject,
            object_standard_name=args.object,
            tenant_id=args.tenant_id,
            graph_client=graph_client,
        )
    elif args.command == "reject":
        await cmd_reject(
            review_conn=review_conn, review_id=args.review_id,
            tenant_id=args.tenant_id, note=args.note,
        )
```

（文件顶部 import 不变，`cmd_list`/`cmd_approve`/`cmd_reject`/`_parse_args`/`_main` 之外的部分——`if __name__ == "__main__":` 那两行——保持原样。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/graphrag/test_review_cli.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/graphrag/review_cli.py tests/graphrag/test_review_cli.py
git commit -m "fix: add required --tenant-id to graph review CLI"
```

---

### Task 5: 文档管理后端 —— 上传/列表/删除/任务状态

**Files:**
- Modify: `app/api/deps.py`
- Modify: `app/config/settings.py`
- Create: `app/api/admin_document_routes.py`
- Modify: `app/main.py`
- Create: `tests/api/test_admin_document_routes.py`

**Interfaces:**
- Consumes: `list_tracked_files`/`remove_tracked_file`（`app/ingestion/tracking.py`）、`enqueue_ingestion_job`/`list_pending_jobs`/`process_pending_jobs`（`app/ingestion/ingestion_queue.py`，Task 2 产出）、`deps.require_admin_session`（Task 4）、`vector_store.delete_by_source`
- Produces: `POST /admin/documents`（上传）、`GET /admin/documents`（列表）、`DELETE /admin/documents`（删除，file_path 作 query 参数）、`GET /admin/documents/jobs/{job_id}`（任务状态）

先补一个 `deps.py` 依赖（本任务需要，之前没有）：`get_ingestion_conn`（进程内单例，模式同 `get_memory_conn`，复用 `app/ingestion/tracking.py::ensure_tracking_schema` + `app/ingestion/ingestion_queue.py::ensure_ingestion_queue_schema`）。

- [ ] **Step 1: `deps.py` 加 `get_ingestion_conn`**

在 `app/api/deps.py` 顶部 import 加：

```python
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.ingestion.tracking import ensure_tracking_schema
```

文件末尾加：

```python
_ingestion_conn_cache: aiosqlite.Connection | None = None
_ingestion_conn_lock = asyncio.Lock()


async def get_ingestion_conn(
    settings: Settings = Depends(get_settings),
) -> aiosqlite.Connection:
    """进程内单例 SQLite 连接，模式同 get_memory_conn。"""
    global _ingestion_conn_cache
    if _ingestion_conn_cache is None:
        async with _ingestion_conn_lock:
            if _ingestion_conn_cache is None:
                from pathlib import Path

                db_path = Path(settings.ingestion_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                await ensure_tracking_schema(conn)
                await ensure_ingestion_queue_schema(conn)
                _ingestion_conn_cache = conn
    return _ingestion_conn_cache
```

把 `"get_ingestion_conn"` 加进 `__all__`。

- [ ] **Step 2: 写失败测试**

```python
# tests/api/test_admin_document_routes.py
import io

import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.ingestion.tracking import ensure_tracking_schema, record_ingested
from app.main import app
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.retrieval.vector_store import InMemoryVectorStore


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        admin_token="tok",
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _ingestion_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)
    return conn


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


def test_upload_without_session_returns_401(tmp_path):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        response = client.post(
            "/admin/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "t1", "build_graph": "false"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_upload_rejects_file_larger_than_100mb(tmp_path):
    import asyncio

    session_store = AdminSessionStore()
    ingestion_conn = asyncio.run(_ingestion_conn())
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    upload_dir = tmp_path / "uploads"
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        oversized = io.BytesIO(b"0" * (101 * 1024 * 1024))
        response = client.post(
            "/admin/documents",
            files={"file": ("big.md", oversized, "text/markdown")},
            data={"tenant_id": "t1", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 413


def test_upload_enqueues_job_and_returns_job_id(tmp_path):
    import asyncio

    session_store = AdminSessionStore()
    ingestion_conn = asyncio.run(_ingestion_conn())
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider())
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    upload_dir = tmp_path / "uploads"
    app.dependency_overrides[deps.get_upload_dir] = lambda: upload_dir
    try:
        client = TestClient(app)
        response = client.post(
            "/admin/documents",
            files={"file": ("a.md", b"## t\ncontent", "text/markdown")},
            data={"tenant_id": "t1", "build_graph": "false"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "job_id" in response.json()
    assert upload_dir.exists()
    assert len(list(upload_dir.glob("*a.md"))) == 1


def test_list_documents_returns_tracked_files_for_tenant(tmp_path):
    import asyncio

    async def _seed():
        conn = await _ingestion_conn()
        await record_ingested(
            conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=3
        )
        return conn

    ingestion_conn = asyncio.run(_seed())
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/admin/documents", params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["documents"][0]["file_path"] == "a.md"
    assert body["documents"][0]["chunk_count"] == 3


def test_delete_document_removes_tracking_and_vectors():
    import asyncio

    async def _seed():
        conn = await _ingestion_conn()
        await record_ingested(
            conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
        )
        return conn

    ingestion_conn = asyncio.run(_seed())
    vector_store = InMemoryVectorStore()

    from app.retrieval.vector_store import VectorRecord

    asyncio.run(
        vector_store.upsert(
            [
                VectorRecord(
                    id="a.md#0", vector=[0.1, 0.2], text="内容",
                    tenant_id="t1", metadata={"source": "a.md"},
                )
            ]
        )
    )

    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: ingestion_conn
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    try:
        client = TestClient(app)
        response = client.request(
            "DELETE", "/admin/documents",
            params={"tenant_id": "t1", "file_path": "a.md"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    remaining = asyncio.run(
        vector_store.search(query_vector=[0.1, 0.2], top_k=10, tenant_id="t1")
    )
    assert remaining == []
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/api/test_admin_document_routes.py -v`
Expected: FAIL（路由/依赖不存在）

- [ ] **Step 4: `deps.py` 加 `get_upload_dir`**

在 `app/config/settings.py` 加一个配置项（`graph_review_db_path` 字段附近）：

```python
    # 后台管理系统上传文件的落盘目录，摄取任务队列按 file_path 读取磁盘
    # 文件（不是直接存字节到数据库），见 app/api/admin_document_routes.py。
    upload_dir: str = "data/uploads"
```

`app/api/deps.py` 文件末尾加：

```python
def get_upload_dir(settings: Settings = Depends(get_settings)) -> "Path":
    from pathlib import Path

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir
```

把 `"get_upload_dir"` 加进 `__all__`；文件顶部 import 加 `from pathlib import Path`（用于类型标注，去掉函数体内的局部 import，直接用顶层 `Path`）。

- [ ] **Step 5: 实现 `app/api/admin_document_routes.py`**

```python
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.ingestion.ingestion_queue import (
    enqueue_ingestion_job,
    list_pending_jobs,
    process_pending_jobs,
)
from app.ingestion.tracking import compute_file_hash, list_tracked_files, remove_tracked_file
from app.providers.embedding import EmbeddingRegistry
from app.retrieval.vector_store import VectorStore

router = APIRouter(prefix="/admin/documents", dependencies=[Depends(deps.require_admin_session)])

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB


class DocumentsListResponse(BaseModel):
    documents: list[dict]
    pending_jobs: list[dict]


class UploadResponse(BaseModel):
    job_id: str


async def _run_pending_jobs(
    ingestion_conn: aiosqlite.Connection,
    embedding_registry: EmbeddingRegistry,
    vector_store: VectorStore,
) -> None:
    """后台任务：入队后立即处理一批，不等外部 cron。"""
    await process_pending_jobs(
        ingestion_conn,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
    )


@router.post("", response_model=UploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    tenant_id: str = Form(...),
    build_graph: bool = Form(False),
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
) -> UploadResponse:
    # tenant_id/build_graph 必须显式标 Form(...)：混用 UploadFile 和裸标量参数时
    # FastAPI 默认把裸标量参数当 query 参数解析，不会去读 multipart body 里
    # 同名的表单字段——前端是把这两个值和文件一起放进同一个 FormData 提交的
    # （见 Task 8 DocumentsPage.tsx），不标 Form(...) 会导致后端读到 422。
    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 100MB 上限")

    tenant_dir = upload_dir / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    dest_path = tenant_dir / f"{uuid.uuid4().hex}_{file.filename}"
    dest_path.write_bytes(contents)

    content_hash = compute_file_hash(dest_path)
    job_id = await enqueue_ingestion_job(
        ingestion_conn,
        tenant_id=tenant_id,
        file_path=str(dest_path),
        content_hash=content_hash,
        action="ingest",
        build_graph=build_graph,
    )
    background_tasks.add_task(
        _run_pending_jobs, ingestion_conn, embedding_registry, vector_store
    )
    return UploadResponse(job_id=job_id)


@router.get("", response_model=DocumentsListResponse)
async def list_documents(
    tenant_id: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentsListResponse:
    documents = await list_tracked_files(ingestion_conn, tenant_id=tenant_id)
    all_pending = await list_pending_jobs(ingestion_conn, limit=50)
    pending_jobs = [job for job in all_pending if job["tenant_id"] == tenant_id]
    return DocumentsListResponse(documents=documents, pending_jobs=pending_jobs)


@router.delete("")
async def delete_document(
    tenant_id: str,
    file_path: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    vector_store: VectorStore = Depends(deps.get_vector_store),
) -> dict[str, bool]:
    await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
    await remove_tracked_file(ingestion_conn, tenant_id=tenant_id, file_path=file_path)
    return {"deleted": True}
```

- [ ] **Step 6: 挂载到 `app/main.py`**

```python
from app.api.admin_document_routes import router as admin_document_router
```

```python
app.include_router(admin_document_router)
```

- [ ] **Step 7: 运行确认通过**

Run: `python -m pytest tests/api/test_admin_document_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 8: Commit**

```bash
git add app/config/settings.py app/api/deps.py app/api/admin_document_routes.py app/main.py tests/api/test_admin_document_routes.py
git commit -m "feat: add admin document upload/list/delete endpoints"
```

---

### Task 6: 知识图谱审核后端 —— 待审核/历史/批准/驳回

**Files:**
- Modify: `app/api/deps.py`
- Create: `app/api/admin_graph_review_routes.py`
- Modify: `app/main.py`
- Create: `tests/api/test_admin_graph_review_routes.py`

**Interfaces:**
- Consumes: `list_pending_reviews`/`list_resolved_reviews`/`approve_review`/`reject_review`（Task 1 产出）、`deps.require_admin_session`（Task 4）
- Produces: `GET /admin/graph-reviews`（`status=pending|approved|rejected`，默认 `pending`）、`POST /admin/graph-reviews/{review_id}/approve`、`POST /admin/graph-reviews/{review_id}/reject`

- [ ] **Step 1: `deps.py` 加 `get_review_conn`**

顶部 import 加 `from app.graphrag.review_queue import ensure_review_schema`。文件末尾加：

```python
_review_conn_cache: aiosqlite.Connection | None = None
_review_conn_lock = asyncio.Lock()


async def get_review_conn(
    settings: Settings = Depends(get_settings),
) -> aiosqlite.Connection:
    """进程内单例 SQLite 连接，模式同 get_memory_conn。"""
    global _review_conn_cache
    if _review_conn_cache is None:
        async with _review_conn_lock:
            if _review_conn_cache is None:
                db_path = Path(settings.graph_review_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                await ensure_review_schema(conn)
                _review_conn_cache = conn
    return _review_conn_cache
```

加进 `__all__`。

- [ ] **Step 2: 写失败测试**

```python
# tests/api/test_admin_graph_review_routes.py
import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.config.settings import Settings
from app.graphrag.review_queue import enqueue_for_review, ensure_review_schema
from app.main import app


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        admin_token="tok",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _authed_headers(session_store: AdminSessionStore) -> dict[str, str]:
    token = session_store.create_session()
    return {"Authorization": f"Bearer {token}"}


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(self, **kwargs) -> None:
        self.written.append(kwargs)


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    return conn


def test_list_pending_reviews_returns_tenant_scoped_rows():
    import asyncio

    async def _seed():
        conn = await _review_conn()
        await enqueue_for_review(
            conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
        return conn

    review_conn = asyncio.run(_seed())
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/admin/graph-reviews", params={"tenant_id": "t1"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(response.json()["reviews"]) == 1


def test_approve_review_calls_graph_client_and_moves_to_history():
    import asyncio

    async def _seed():
        conn = await _review_conn()
        review_id = await enqueue_for_review(
            conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
        return conn, review_id

    review_conn, review_id = asyncio.run(_seed())
    session_store = AdminSessionStore()
    graph_client = FakeGraphClient()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    try:
        client = TestClient(app)
        response = client.post(
            f"/admin/graph-reviews/{review_id}/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert graph_client.written == [
        {
            "subject_standard_name": "A", "object_standard_name": "B",
            "relation_type": "RELATED_TO", "source": "s.md", "tenant_id": "t1",
        }
    ]

    history_response = TestClient(app)
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        response = history_response.get(
            "/admin/graph-reviews", params={"tenant_id": "t1", "status": "approved"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()
    assert len(response.json()["reviews"]) == 1


def test_reject_review_marks_rejected():
    import asyncio

    async def _seed():
        conn = await _review_conn()
        review_id = await enqueue_for_review(
            conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
        return conn, review_id

    review_conn, review_id = asyncio.run(_seed())
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.post(
            f"/admin/graph-reviews/{review_id}/reject",
            json={"tenant_id": "t1", "note": "噪声"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_approve_nonexistent_review_returns_404():
    session_store = AdminSessionStore()
    review_conn = None
    import asyncio

    review_conn = asyncio.run(_review_conn())
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    try:
        client = TestClient(app)
        response = client.post(
            "/admin/graph-reviews/999/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/api/test_admin_graph_review_routes.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 `app/api/admin_graph_review_routes.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    approve_review,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)

router = APIRouter(
    prefix="/admin/graph-reviews", dependencies=[Depends(deps.require_admin_session)]
)


class ReviewListResponse(BaseModel):
    reviews: list[dict]


class ApproveRequest(BaseModel):
    tenant_id: str
    subject_standard_name: str
    object_standard_name: str


class RejectRequest(BaseModel):
    tenant_id: str
    note: str | None = None


@router.get("", response_model=ReviewListResponse)
async def list_reviews(
    tenant_id: str,
    status: str = "pending",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> ReviewListResponse:
    if status == "pending":
        reviews = await list_pending_reviews(review_conn, tenant_id=tenant_id)
    elif status in ("approved", "rejected"):
        reviews = await list_resolved_reviews(review_conn, tenant_id=tenant_id, status=status)
    else:
        raise HTTPException(status_code=400, detail="status 必须是 pending/approved/rejected")
    return ReviewListResponse(reviews=reviews)


@router.post("/{review_id}/approve")
async def approve(
    review_id: int,
    payload: ApproveRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    try:
        await approve_review(
            review_conn,
            review_id=review_id,
            subject_standard_name=payload.subject_standard_name,
            object_standard_name=payload.object_standard_name,
            tenant_id=payload.tenant_id,
            graph_client=graph_client,
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    return {"approved": True}


@router.post("/{review_id}/reject")
async def reject(
    review_id: int,
    payload: RejectRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    try:
        await reject_review(
            review_conn, review_id=review_id, tenant_id=payload.tenant_id, note=payload.note
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    return {"rejected": True}
```

- [ ] **Step 5: 挂载到 `app/main.py`**

```python
from app.api.admin_graph_review_routes import router as admin_graph_review_router
```

```python
app.include_router(admin_graph_review_router)
```

- [ ] **Step 6: 运行确认通过**

Run: `python -m pytest tests/api/test_admin_graph_review_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 运行全量后端测试确认没有连带破坏**

Run: `python -m pytest -v`
Expected: 全部 PASS

- [ ] **Step 8: Commit**

```bash
git add app/api/deps.py app/api/admin_graph_review_routes.py app/main.py tests/api/test_admin_graph_review_routes.py
git commit -m "feat: add admin graph review endpoints (list/approve/reject with history)"
```

---

### Task 7: 前端引入 react-router，搭登录页 + 管理后台外壳

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/pages/ChatPage.tsx`
- Create: `frontend/src/admin/AdminLayout.tsx`
- Create: `frontend/src/admin/LoginPage.tsx`
- Create: `frontend/src/admin/useAdminAuth.ts`
- Create: `frontend/src/admin/TenantSwitcher.tsx`
- Create: `frontend/src/admin/useAdminTenant.ts`
- Modify: `frontend/DESIGN.md`

**Interfaces:**
- Produces: `useAdminAuth()` hook（`sessionToken: string | null`、`login(adminToken: string) => Promise<void>`、`logout: () => void`）；路由 `/`、`/admin/login`、`/admin`（重定向到 `/admin/documents`，Task 8 补真实内容）。

- [ ] **Step 1: 安装 react-router-dom**

```bash
cd frontend && npm install react-router-dom
```

- [ ] **Step 2: 把现有 `App.tsx` 内容搬到 `pages/ChatPage.tsx`**

`frontend/src/pages/ChatPage.tsx`（内容基本是当前 `App.tsx` 减去 `<div className="flex min-h-dvh ...">` 外壳，因为外壳要在新的 `App.tsx` 里统一处理路由）：

```tsx
import { Link } from 'react-router-dom'
import { Hero } from '../components/Hero'
import { ChatWindow } from '../components/ChatWindow'
import { ChatInput } from '../components/ChatInput'
import { Footer } from '../components/Footer'
import { useAgentChat } from '../hooks/useAgentChat'

export function ChatPage() {
  const { messages, isSending, sendQuestion, resetConversation } = useAgentChat()

  return (
    <div className="flex min-h-dvh flex-col bg-paper">
      <div className="border-b-2 border-ink bg-ink px-4 py-2 text-center font-mono text-xs uppercase tracking-widest text-accent-yellow">
        检索增强生成 + 知识图谱驱动的客服问答演示
      </div>
      <nav className="flex items-center justify-between border-b-2 border-ink bg-accent-yellow px-6 py-4">
        <span className="font-bold text-ink">客服智能问答 Demo</span>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={resetConversation}
            disabled={messages.length === 0}
            className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
          >
            重新开始对话
          </button>
          <Link
            to="/admin"
            className="flex min-h-[44px] cursor-pointer items-center border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
          >
            ⚙️ 管理后台
          </Link>
        </div>
      </nav>
      <Hero />
      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col">
        <ChatWindow messages={messages} />
        <ChatInput disabled={isSending} onSend={sendQuestion} />
      </main>
      <Footer />
    </div>
  )
}
```

- [ ] **Step 3: 写 `useAdminAuth` hook**

```tsx
// frontend/src/admin/useAdminAuth.ts
import { useCallback, useState } from 'react'

const SESSION_STORAGE_KEY = 'admin_session_token'

export function useAdminAuth() {
  const [sessionToken, setSessionToken] = useState<string | null>(() =>
    sessionStorage.getItem(SESSION_STORAGE_KEY),
  )

  const login = useCallback(async (adminToken: string) => {
    const response = await fetch('/admin/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ admin_token: adminToken }),
    })
    if (!response.ok) {
      throw new Error('管理员 token 不正确')
    }
    const data = (await response.json()) as { session_token: string }
    sessionStorage.setItem(SESSION_STORAGE_KEY, data.session_token)
    setSessionToken(data.session_token)
  }, [])

  const logout = useCallback(() => {
    sessionStorage.removeItem(SESSION_STORAGE_KEY)
    setSessionToken(null)
  }, [])

  return { sessionToken, login, logout }
}
```

- [ ] **Step 4: 写 `LoginPage.tsx`**

```tsx
// frontend/src/admin/LoginPage.tsx
import { useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'

export function LoginPage() {
  const { sessionToken, login } = useAdminAuth()
  const [adminToken, setAdminToken] = useState('')
  const [error, setError] = useState<string | null>(null)

  if (sessionToken) {
    return <Navigate to="/admin" replace />
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      await login(adminToken)
    } catch {
      setError('管理员 token 不正确')
    }
  }

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-paper px-4">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 border-2 border-ink bg-card p-6 shadow-brutal"
      >
        <h1 className="text-xl font-bold text-ink">管理后台登录</h1>
        <input
          type="password"
          value={adminToken}
          onChange={(event) => setAdminToken(event.target.value)}
          placeholder="管理员 token"
          className="border-2 border-ink bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
        />
        {error && <p className="text-sm text-ink">{error}</p>}
        <button
          type="submit"
          className="cursor-pointer border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none"
        >
          登录
        </button>
      </form>
    </div>
  )
}
```

- [ ] **Step 5: 写 `AdminLayout.tsx`（侧边栏外壳）**

```tsx
// frontend/src/admin/AdminLayout.tsx
import { NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { TenantSwitcher } from './TenantSwitcher'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `border-2 border-ink px-3 py-2.5 text-sm font-bold transition ${
    isActive ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink hover:bg-card'
  }`

export function AdminLayout() {
  const { sessionToken, logout } = useAdminAuth()

  if (!sessionToken) {
    return <Navigate to="/admin/login" replace />
  }

  return (
    <div className="flex min-h-dvh bg-paper">
      <aside className="flex w-56 flex-shrink-0 flex-col justify-between border-r-2 border-ink bg-card p-4">
        <nav className="flex flex-col gap-2">
          <NavLink to="/admin/documents" className={navLinkClass}>
            文档管理
          </NavLink>
          <NavLink to="/admin/graph-reviews" className={navLinkClass}>
            知识图谱审核
          </NavLink>
        </nav>
        <div className="flex flex-col gap-3">
          <TenantSwitcher />
          <a
            href="/"
            className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-center text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
          >
            返回前台
          </a>
          <button
            type="button"
            onClick={logout}
            className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
          >
            登出
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
```

（`TenantSwitcher` 在 Task 8 才创建，这一步先只是引用，Task 7 结束时 typecheck 会失败在预期内——Step 8 会先放一个最小占位实现让 Task 7 能独立跑通，Task 8 再补完整功能。）

- [ ] **Step 6: 先给 `TenantSwitcher` 一个最小可用实现（Task 8 会扩展）**

```tsx
// frontend/src/admin/TenantSwitcher.tsx
import { useAdminTenant } from './useAdminTenant'

export function TenantSwitcher() {
  const { tenantId, setTenantId } = useAdminTenant()

  return (
    <select
      value={tenantId}
      onChange={(event) => setTenantId(event.target.value)}
      className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
    >
      <option value="demo">demo</option>
    </select>
  )
}
```

```tsx
// frontend/src/admin/useAdminTenant.ts
import { useState } from 'react'

const TENANT_STORAGE_KEY = 'admin_current_tenant'

export function useAdminTenant() {
  const [tenantId, setTenantIdState] = useState(
    () => sessionStorage.getItem(TENANT_STORAGE_KEY) ?? 'demo',
  )

  const setTenantId = (next: string) => {
    sessionStorage.setItem(TENANT_STORAGE_KEY, next)
    setTenantIdState(next)
  }

  return { tenantId, setTenantId }
}
```

- [ ] **Step 7: 重写 `App.tsx` 挂路由（Task 8/9 会把占位页替换成真实页面）**

```tsx
import { Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { AdminLayout } from './admin/AdminLayout'
import { LoginPage } from './admin/LoginPage'

function DocumentsPlaceholder() {
  return <p className="text-ink">文档管理页面开发中</p>
}

function GraphReviewsPlaceholder() {
  return <p className="text-ink">知识图谱审核页面开发中</p>
}

function App() {
  return (
    <Routes>
      <Route path="/" element={<ChatPage />} />
      <Route path="/admin/login" element={<LoginPage />} />
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<Navigate to="documents" replace />} />
        <Route path="documents" element={<DocumentsPlaceholder />} />
        <Route path="graph-reviews" element={<GraphReviewsPlaceholder />} />
      </Route>
    </Routes>
  )
}

export default App
```

- [ ] **Step 8: `main.tsx` 包一层 `BrowserRouter`**

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import '@fontsource/space-grotesk/400.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/space-mono/400.css'
import '@fontsource/space-mono/700.css'
import './styles/index.css'

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('未找到 #root 挂载节点')
}

createRoot(rootElement).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
```

- [ ] **Step 9: 删除旧的 `frontend/src/App.tsx` 里已经搬走的逻辑残留（确认没有重复定义），运行 typecheck**

Run: `cd frontend && npm run typecheck`
Expected: PASS（如果报 `TenantSwitcher`/`useAdminTenant` 相关错误，检查 Step 6 文件路径是否正确创建）

- [ ] **Step 10: 更新 `DESIGN.md` 记录新增的路由/管理后台样式约定**

在 `frontend/DESIGN.md` 第 6 节末尾追加一小节：

```markdown
### 6.10 管理后台侧边栏导航项（`AdminLayout.tsx`）

未激活态：`border-2 border-ink px-3 py-2.5 text-sm font-bold bg-paper text-ink`；
激活态（当前路由）：`bg-accent-pink text-ink shadow-brutal-sm`（复用主按钮的强调色，
不新增颜色 token）。用 react-router 的 `NavLink` 的 `isActive` 判断，不手动比较路径字符串。
```

- [ ] **Step 11: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/main.tsx frontend/src/App.tsx frontend/src/pages/ChatPage.tsx frontend/src/admin frontend/DESIGN.md
git commit -m "feat(frontend): add react-router, admin login page and sidebar layout shell"
```

---

### Task 8: 前端文档管理页

**Files:**
- Create: `frontend/src/admin/DocumentsPage.tsx`
- Create: `frontend/src/admin/adminApi.ts`
- Modify: `frontend/src/App.tsx`（把 `DocumentsPlaceholder` 换成真实组件）

**Interfaces:**
- Consumes: `useAdminAuth`（Task 7）、`useAdminTenant`（Task 7）
- Produces: `adminFetch(path, options, sessionToken) => Promise<Response>`（统一带 `Authorization` 头的 fetch 包装，Task 9 复用）

- [ ] **Step 1: 写 `adminApi.ts` 统一请求封装**

```tsx
// frontend/src/admin/adminApi.ts
export async function adminFetch(
  path: string,
  sessionToken: string,
  options: RequestInit = {},
): Promise<Response> {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...options.headers,
      Authorization: `Bearer ${sessionToken}`,
    },
  })
  if (response.status === 401) {
    sessionStorage.removeItem('admin_session_token')
    window.location.href = '/admin/login'
    throw new Error('登录已过期')
  }
  return response
}
```

- [ ] **Step 2: 写 `DocumentsPage.tsx`**

```tsx
// frontend/src/admin/DocumentsPage.tsx
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './useAdminTenant'

interface TrackedDocument {
  file_path: string
  content_hash: string
  chunk_count: number
}

interface PendingJob {
  job_id: string
  file_path: string
  status: string
  last_error: string | null
}

export function DocumentsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [documents, setDocuments] = useState<TrackedDocument[]>([])
  const [pendingJobs, setPendingJobs] = useState<PendingJob[]>([])
  const [buildGraph, setBuildGraph] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/admin/documents?tenant_id=${encodeURIComponent(tenantId)}`,
      sessionToken,
    )
    const data = (await response.json()) as {
      documents: TrackedDocument[]
      pending_jobs: PendingJob[]
    }
    setDocuments(data.documents)
    setPendingJobs(data.pending_jobs)
  }, [sessionToken, tenantId])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 3000)
    return () => clearInterval(timer)
  }, [refresh])

  const handleUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!sessionToken) return
    const form = event.currentTarget
    const fileInput = form.elements.namedItem('file') as HTMLInputElement
    const file = fileInput.files?.[0]
    if (!file) return

    setUploading(true)
    setError(null)
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('tenant_id', tenantId)
      formData.append('build_graph', String(buildGraph))
      const response = await adminFetch('/admin/documents', sessionToken, {
        method: 'POST',
        body: formData,
      })
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string }
        throw new Error(body.detail ?? '上传失败')
      }
      form.reset()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (filePath: string) => {
    if (!sessionToken) return
    await adminFetch(
      `/admin/documents?tenant_id=${encodeURIComponent(tenantId)}&file_path=${encodeURIComponent(filePath)}`,
      sessionToken,
      { method: 'DELETE' },
    )
    await refresh()
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">文档管理（租户：{tenantId}）</h1>

      <form
        onSubmit={handleUpload}
        className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
      >
        <input type="file" name="file" required className="text-ink" />
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={buildGraph}
            onChange={(event) => setBuildGraph(event.target.checked)}
          />
          同时构建知识图谱（LLM 关系抽取，耗时更久）
        </label>
        {error && <p className="text-sm text-ink">{error}</p>}
        <button
          type="submit"
          disabled={uploading}
          className="min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
        >
          {uploading ? '上传中…' : '上传文档'}
        </button>
      </form>

      {pendingJobs.length > 0 && (
        <div className="flex flex-col gap-2">
          <h2 className="font-bold text-ink">处理中的任务</h2>
          {pendingJobs.map((job) => (
            <div
              key={job.job_id}
              className="border border-ink bg-accent-yellow px-3 py-2 text-sm text-ink shadow-brutal-sm"
            >
              {job.file_path} — {job.status}
              {job.last_error && <span className="text-status-error"> ({job.last_error})</span>}
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-col gap-2">
        <h2 className="font-bold text-ink">已摄取文档</h2>
        {documents.map((doc) => (
          <div
            key={doc.file_path}
            className="flex items-center justify-between border-2 border-ink bg-card px-4 py-3 shadow-brutal-sm"
          >
            <span className="text-ink">
              {doc.file_path}（{doc.chunk_count} chunks）
            </span>
            <button
              type="button"
              onClick={() => handleDelete(doc.file_path)}
              className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
            >
              删除
            </button>
          </div>
        ))}
        {documents.length === 0 && <p className="text-ink-soft">当前租户还没有已摄取的文档。</p>}
      </div>
    </div>
  )
}
```

- [ ] **Step 3: 在 `App.tsx` 里把占位组件换成真实页面**

```tsx
import { DocumentsPage } from './admin/DocumentsPage'
```

把 `<Route path="documents" element={<DocumentsPlaceholder />} />` 改成
`<Route path="documents" element={<DocumentsPage />} />`，删掉 `DocumentsPlaceholder` 函数定义。

- [ ] **Step 4: 运行 typecheck**

Run: `cd frontend && npm run typecheck`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/DocumentsPage.tsx frontend/src/admin/adminApi.ts frontend/src/App.tsx
git commit -m "feat(frontend): add admin documents page (upload/list/delete)"
```

---

### Task 9: 前端知识图谱审核页

**Files:**
- Create: `frontend/src/admin/GraphReviewsPage.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: `adminFetch`（Task 8）、`useAdminAuth`/`useAdminTenant`（Task 7）

- [ ] **Step 1: 写 `GraphReviewsPage.tsx`**

```tsx
// frontend/src/admin/GraphReviewsPage.tsx
import { useCallback, useEffect, useState } from 'react'
import { adminFetch } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './useAdminTenant'

interface PendingReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  suggested_subject_standard_name: string | null
  suggested_object_standard_name: string | null
}

interface ResolvedReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  status: string
  resolved_at: string
  resolved_note: string | null
}

type Tab = 'pending' | 'history'

export function GraphReviewsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [tab, setTab] = useState<Tab>('pending')
  const [pending, setPending] = useState<PendingReview[]>([])
  const [history, setHistory] = useState<ResolvedReview[]>([])
  const [drafts, setDrafts] = useState<Record<number, { subject: string; object: string }>>({})

  const refreshPending = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=pending`,
      sessionToken,
    )
    const data = (await response.json()) as { reviews: PendingReview[] }
    setPending(data.reviews)
    setDrafts(
      Object.fromEntries(
        data.reviews.map((review) => [
          review.review_id,
          {
            subject: review.suggested_subject_standard_name ?? '',
            object: review.suggested_object_standard_name ?? '',
          },
        ]),
      ),
    )
  }, [sessionToken, tenantId])

  const refreshHistory = useCallback(async () => {
    if (!sessionToken) return
    const [approvedRes, rejectedRes] = await Promise.all([
      adminFetch(
        `/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=approved`,
        sessionToken,
      ),
      adminFetch(
        `/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=rejected`,
        sessionToken,
      ),
    ])
    const approved = (await approvedRes.json()) as { reviews: ResolvedReview[] }
    const rejected = (await rejectedRes.json()) as { reviews: ResolvedReview[] }
    setHistory(
      [...approved.reviews, ...rejected.reviews].sort((a, b) =>
        b.resolved_at.localeCompare(a.resolved_at),
      ),
    )
  }, [sessionToken, tenantId])

  useEffect(() => {
    if (tab === 'pending') refreshPending()
    else refreshHistory()
  }, [tab, refreshPending, refreshHistory])

  const handleApprove = async (reviewId: number) => {
    if (!sessionToken) return
    const draft = drafts[reviewId]
    if (!draft?.subject || !draft?.object) return
    await adminFetch(`/admin/graph-reviews/${reviewId}/approve`, sessionToken, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: tenantId,
        subject_standard_name: draft.subject,
        object_standard_name: draft.object,
      }),
    })
    await refreshPending()
  }

  const handleReject = async (reviewId: number) => {
    if (!sessionToken) return
    await adminFetch(`/admin/graph-reviews/${reviewId}/reject`, sessionToken, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId }),
    })
    await refreshPending()
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">知识图谱审核（租户：{tenantId}）</h1>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => setTab('pending')}
          className={`min-h-[44px] cursor-pointer border-2 border-ink px-4 py-2 text-sm font-bold transition ${
            tab === 'pending' ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink'
          }`}
        >
          待审核
        </button>
        <button
          type="button"
          onClick={() => setTab('history')}
          className={`min-h-[44px] cursor-pointer border-2 border-ink px-4 py-2 text-sm font-bold transition ${
            tab === 'history' ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink'
          }`}
        >
          历史记录
        </button>
      </div>

      {tab === 'pending' &&
        pending.map((review) => (
          <div
            key={review.review_id}
            className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
          >
            <p className="text-sm text-ink-soft">
              候选：{review.subject_candidate} —[{review.relation_type}]→{' '}
              {review.object_candidate}（原因：{review.reason}）
            </p>
            <div className="flex gap-3">
              <input
                value={drafts[review.review_id]?.subject ?? ''}
                onChange={(event) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      subject: event.target.value,
                    },
                  }))
                }
                placeholder="subject 标准名"
                className="flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
              />
              <input
                value={drafts[review.review_id]?.object ?? ''}
                onChange={(event) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      object: event.target.value,
                    },
                  }))
                }
                placeholder="object 标准名"
                className="flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
              />
            </div>
            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => handleApprove(review.review_id)}
                disabled={!drafts[review.review_id]?.subject || !drafts[review.review_id]?.object}
                className="min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
              >
                批准
              </button>
              <button
                type="button"
                onClick={() => handleReject(review.review_id)}
                className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
              >
                驳回
              </button>
            </div>
          </div>
        ))}
      {tab === 'pending' && pending.length === 0 && (
        <p className="text-ink-soft">当前没有待审核的候选关系。</p>
      )}

      {tab === 'history' &&
        history.map((review) => (
          <div
            key={review.review_id}
            className="flex flex-col gap-1 border-2 border-ink bg-card p-4 shadow-brutal-sm"
          >
            <p className="text-sm text-ink">
              {review.subject_candidate} —[{review.relation_type}]→ {review.object_candidate}
            </p>
            <p className="text-xs text-ink-soft">
              {review.status === 'approved' ? '已批准' : '已驳回'} · {review.resolved_at}
              {review.resolved_note && ` · ${review.resolved_note}`}
            </p>
          </div>
        ))}
      {tab === 'history' && history.length === 0 && (
        <p className="text-ink-soft">还没有处理过的记录。</p>
      )}
    </div>
  )
}
```

- [ ] **Step 2: 在 `App.tsx` 里把占位组件换成真实页面**

```tsx
import { GraphReviewsPage } from './admin/GraphReviewsPage'
```

把 `<Route path="graph-reviews" element={<GraphReviewsPlaceholder />} />` 改成
`<Route path="graph-reviews" element={<GraphReviewsPage />} />`，删掉 `GraphReviewsPlaceholder` 函数定义。

- [ ] **Step 3: 运行 typecheck**

Run: `cd frontend && npm run typecheck`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/admin/GraphReviewsPage.tsx frontend/src/App.tsx
git commit -m "feat(frontend): add admin graph review page (approve/reject with history tab)"
```

---

### Task 10: 全流程集成验证

**Files:**
- 无新增/修改文件（纯验证任务）

- [ ] **Step 1: 后端全量测试**

Run: `python -m pytest -v`
Expected: 全部 PASS

- [ ] **Step 2: 前端构建检查**

Run: `cd frontend && npm run build`
Expected: 构建成功

- [ ] **Step 3: 配置管理员 token 并启动服务**

在仓库根目录 `.env` 加一行 `CUSTOMER_RAG_ADMIN_TOKEN=local-dev-admin-token`（仅本地开发用，不要提交真实生产 token 到 `.env`）。

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
cd frontend && npm run dev
```

- [ ] **Step 4: 浏览器人工核验清单**

1. 打开 `http://localhost:5173/`，导航栏右侧能看到"⚙️ 管理后台"按钮，点击跳转到 `/admin/login`。
2. 输入错误 token 登录，看到明确的错误提示；输入 `.env` 里配置的正确 token，登录成功跳转到 `/admin/documents`。
3. 侧边栏"文档管理"/"知识图谱审核"两项能正常切换，当前项高亮（粉色底）。
4. 文档管理页上传一个小的 `.md` 文件（不勾选"构建知识图谱"），几秒内"处理中的任务"消失、"已摄取文档"列表出现新记录。
5. 上传一个会命中术语表模糊匹配/未对齐场景的文档并勾选"构建知识图谱"（可以直接用 `docs/demo-data/` 下的示例文件），等待任务完成后去"知识图谱审核"页的"待审核"tab，能看到候选记录，输入框预填了建议标准名（如果有）。
6. 修改标准名后点"批准"，记录从待审核列表消失；切到"历史记录"tab 能看到这条记录标记为"已批准"。
7. 对另一条待审核记录点"驳回"，同样从待审核消失、历史记录里显示"已驳回"。
8. 文档管理页删除一条已摄取文档，列表里消失，刷新页面确认没有重新出现。
9. 点"登出"，被踢回登录页；关闭浏览器标签页重新打开 `http://localhost:5173/admin`，因为 `sessionStorage` 已清空，同样被重定向到登录页（验证不是"记住登录"）。
10. 用浏览器 DevTools 直接调用 `fetch('/admin/documents?tenant_id=demo')`（不带 `Authorization` 头），确认返回 401，不是意外放行。

若发现任何一项不符，记录具体现象，回到对应任务的文件修正，重新走该任务的测试+提交步骤。

- [ ] **Step 5: 停止开发服务器**

核验完成后 Ctrl+C 停止 `npm run dev` 和 `uvicorn`，本任务本身不产生代码改动，不需要提交。
