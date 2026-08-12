# 知识图谱审核翻页 + 关系抽取实体范围约束 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给知识图谱审核后台的"待审核"和"历史记录"两个列表加上真正可用的服务端分页；同时给 LLM 关系抽取的 system prompt 补一段"什么算专有名词"的范围约束，把 relation_type 那一侧已有的范例+反例模式对称地补到 subject/object 这一侧。

**Architecture:** 两个互不依赖的改动打包在一份计划里。（A）分页：`GET /api/admin/graph-reviews` 现状要么无上限全量返回（`list_pending_reviews`），要么隐性截断在 50 条却没有翻页入口（`list_resolved_reviews` 虽然已经支持 `limit/offset` 但路由层没有暴露成查询参数）——这是项目里第一个要做真正翻页的列表接口，没有现成模式可抄，需要从查询层到路由层到前端组件全新设计。（B）抽取范围：`app/graphrag/llm_extractor.py` 的 prompt 对 relation_type 给了 10 个类别+范例，但对 subject/object（什么算"专有名词"）只有一句话、零约束——这是抽取阶段唯一没有边界的部分，改动范围仅限这一段 prompt 文本，不涉及归一化/审核架构（"封闭词表+人工审核"的整体设计维持不变）。A、B 两部分代码互不接触，可以任意顺序执行或并行执行。

**Tech Stack:** 后端 FastAPI + aiosqlite，测试用 pytest（已有完整测试基础设施）。前端 React + TypeScript + Vite——**项目目前没有配置任何前端自动化测试框架**（`frontend/package.json` 的 `devDependencies` 里没有 vitest/jest/@testing-library），前端任务的验证手段是 `npm run typecheck` + `npm run build` + 手动/浏览器核对，不要在计划外臆造测试命令。

## Global Constraints

- 后端每个任务做完都要跑一次该任务改动的测试文件确认通过；全部任务做完后跑一次 `tests/` 全量测试，确认没有引入新的失败。已知有 1 个与本计划完全无关的预存失败 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`（环境变量污染导致，早于本计划就存在），不用管它，只要不新增失败即可。
- 所有新增/修改的 SQL 查询必须保留现状的 `tenant_id` 过滤条件，不能引入跨租户数据可见性问题。
- 前端改动完成后必须跑 `npm run typecheck`（`frontend/` 目录下）和 `npm run build`，两者都必须无错误退出；没有自动化测试可跑，不要编造。
- Prompt 文本改动延续 `app/graphrag/llm_extractor.py` 现有的引号风格：叙述性整句用双引号 Python 字符串，含有需要展示引号的示例短语（如 `"大床房"`）的行用单引号 Python 字符串包裹、内嵌 ASCII 双引号。
- 每个任务改完就提交一次，不要攒到最后一起提交。

---

## Task 1: 审核队列查询层加分页支持

**Files:**
- Modify: `app/graphrag/review_queue.py:54-171`（`ensure_review_schema` 加索引迁移；`list_pending_reviews` 加 `limit/offset`；新增 `count_pending_reviews`/`count_resolved_reviews`）
- Test: `tests/graphrag/test_review_queue.py`

**Interfaces:**
- Produces:
  - `list_pending_reviews(conn, *, tenant_id: str, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]`（签名变化：新增两个可选参数，`limit=None` 时行为与改动前完全一致——返回该租户全部待审核记录，`review_cli.py::cmd_list` 等既有调用方不用改）
  - `count_pending_reviews(conn, *, tenant_id: str) -> int`
  - `count_resolved_reviews(conn, *, tenant_id: str, status: str | None = None) -> int`（`status=None` 语义与 `list_resolved_reviews` 一致：统计 approved+rejected 两种之和）

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_review_queue.py` 顶部的 import 块里加上两个新函数：

```python
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    approve_review,
    count_pending_reviews,
    count_resolved_reviews,
    enqueue_for_review,
    ensure_review_schema,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)
```

在文件末尾追加以下测试：

```python
async def test_list_pending_reviews_respects_limit_and_offset():
    conn = await _connect()
    for i in range(5):
        await enqueue_for_review(
            conn, subject_candidate=f"s{i}", object_candidate=f"o{i}", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )

    page1 = await list_pending_reviews(conn, tenant_id="t1", limit=2, offset=0)
    page2 = await list_pending_reviews(conn, tenant_id="t1", limit=2, offset=2)

    assert [r["subject_candidate"] for r in page1] == ["s0", "s1"]
    assert [r["subject_candidate"] for r in page2] == ["s2", "s3"]


async def test_list_pending_reviews_without_limit_returns_everything():
    conn = await _connect()
    for i in range(3):
        await enqueue_for_review(
            conn, subject_candidate=f"s{i}", object_candidate=f"o{i}", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )

    pending = await list_pending_reviews(conn, tenant_id="t1")

    assert len(pending) == 3


async def test_count_pending_reviews_matches_tenant_scoped_total():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await enqueue_for_review(
        conn, subject_candidate="c", object_candidate="d", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await enqueue_for_review(
        conn, subject_candidate="e", object_candidate="f", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t2",
    )

    assert await count_pending_reviews(conn, tenant_id="t1") == 2
    assert await count_pending_reviews(conn, tenant_id="t2") == 1


async def test_count_resolved_reviews_matches_status_filter():
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
        tenant_id="t1", graph_client=graph_client, now=_NOW,
    )
    await reject_review(conn, review_id=rejected_id, tenant_id="t1", note="噪声")

    assert await count_resolved_reviews(conn, tenant_id="t1") == 2
    assert await count_resolved_reviews(conn, tenant_id="t1", status="approved") == 1
    assert await count_resolved_reviews(conn, tenant_id="t1", status="rejected") == 1
```

（`_connect`、`_NOW`、`FakeGraphClient` 这几个 fixture/helper 在这个测试文件里已经存在，直接复用，不用重新定义。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py -q`
Expected: FAIL，报 `ImportError: cannot import name 'count_pending_reviews'`（因为这两个函数还不存在）。

- [ ] **Step 3: 实现**

在 `app/graphrag/review_queue.py` 里，把 `ensure_review_schema` 函数体内最后一段索引创建（当前是第 77-80 行）改成：

```python
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_review_queue_tenant_status "
        "ON graph_review_queue (tenant_id, status)"
    )
    # 分页查询历史记录时 ORDER BY resolved_at DESC 需要排序，上面的
    # (tenant_id, status) 索引只能命中过滤条件、排序仍需额外一步；这个
    # 三列复合索引让 tenant_id+status 精确匹配的历史记录分页查询可以直接
    # 走索引有序扫描，不用每次都对候选行做一次排序。
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_review_queue_tenant_status_resolved "
        "ON graph_review_queue (tenant_id, status, resolved_at)"
    )
    await conn.commit()
```

把 `list_pending_reviews`（当前第 123-135 行）整个替换成：

```python
async def list_pending_reviews(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """limit=None（默认）返回该租户全部待审核记录，保持 review_cli.py 等
    既有调用方不传这两个参数时的行为不变；管理后台分页时显式传入具体的
    limit/offset。SQLite 的 LIMIT 取负数即表示不限制行数，用 -1 承载
    limit=None 这个语义，不需要为"要不要拼 LIMIT 子句"写分支 SQL。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "source, created_at FROM graph_review_queue "
        "WHERE status = 'pending' AND tenant_id = ? ORDER BY review_id LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
```

在 `list_resolved_reviews` 函数结束之后（当前第 171 行 `return [dict(row) for row in rows]` 之后、`_fetch_pending_row` 定义之前）插入两个新函数：

```python
async def count_pending_reviews(conn: aiosqlite.Connection, *, tenant_id: str) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM graph_review_queue WHERE status = 'pending' AND tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return row[0]


async def count_resolved_reviews(
    conn: aiosqlite.Connection, *, tenant_id: str, status: str | None = None
) -> int:
    """status=None 统计 approved+rejected 两种之和，语义与 list_resolved_reviews 一致。"""
    if status is None:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM graph_review_queue "
            "WHERE tenant_id = ? AND status IN ('approved', 'rejected')",
            (tenant_id,),
        )
    else:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM graph_review_queue WHERE tenant_id = ? AND status = ?",
            (tenant_id, status),
        )
    row = await cursor.fetchone()
    return row[0]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py -q`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/review_queue.py tests/graphrag/test_review_queue.py
git commit -m "feat(graphrag): add pagination support to review queue queries"
```

---

## Task 2: 路由层暴露分页参数 + 返回总数

**Files:**
- Modify: `app/api/admin_graph_review_routes.py`（整个文件，改动集中在 `list_reviews` 函数和 `ReviewListResponse` 模型）
- Test: `tests/api/test_admin_graph_review_routes.py`

**Interfaces:**
- Consumes: Task 1 产出的 `count_pending_reviews`、`count_resolved_reviews`，以及已有的 `list_pending_reviews(..., limit=..., offset=...)`、`list_resolved_reviews(..., limit=..., offset=...)`
- Produces: `GET /api/admin/graph-reviews?tenant_id=...&status=...&page=<int,默认1,>=1>&page_size=<int,默认20,1~100>` → `{"reviews": [...], "total": <int>}`

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_graph_review_routes.py` 末尾追加：

```python
def test_list_pending_reviews_returns_total_count_and_respects_page_size(review_conn):
    for i in range(3):
        asyncio.run(
            enqueue_for_review(
                review_conn, subject_candidate=f"s{i}", object_candidate=f"o{i}",
                relation_type="RELATED_TO", reason="subject_unresolved",
                source="s.md", tenant_id="t1",
            )
        )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/graph-reviews",
            params={"tenant_id": "t1", "status": "pending", "page": 1, "page_size": 2},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["reviews"]) == 2
    assert body["total"] == 3


def test_list_pending_reviews_second_page_returns_remaining_rows(review_conn):
    for i in range(3):
        asyncio.run(
            enqueue_for_review(
                review_conn, subject_candidate=f"s{i}", object_candidate=f"o{i}",
                relation_type="RELATED_TO", reason="subject_unresolved",
                source="s.md", tenant_id="t1",
            )
        )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/graph-reviews",
            params={"tenant_id": "t1", "status": "pending", "page": 2, "page_size": 2},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert len(body["reviews"]) == 1
    assert body["reviews"][0]["subject_candidate"] == "s2"
    assert body["total"] == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_graph_review_routes.py -q`
Expected: FAIL，`assert body["total"] == 3` 处报 `KeyError: 'total'`（路由目前不返回这个字段）。

- [ ] **Step 3: 实现**

把 `app/api/admin_graph_review_routes.py` 整个文件替换成：

```python
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    approve_review,
    count_pending_reviews,
    count_resolved_reviews,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)

router = APIRouter(
    prefix="/api/admin/graph-reviews", dependencies=[Depends(deps.require_admin_session)]
)


class ReviewListResponse(BaseModel):
    reviews: list[dict]
    total: int


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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> ReviewListResponse:
    offset = (page - 1) * page_size
    if status == "pending":
        reviews = await list_pending_reviews(
            review_conn, tenant_id=tenant_id, limit=page_size, offset=offset
        )
        total = await count_pending_reviews(review_conn, tenant_id=tenant_id)
    elif status in ("approved", "rejected"):
        reviews = await list_resolved_reviews(
            review_conn, tenant_id=tenant_id, status=status, limit=page_size, offset=offset
        )
        total = await count_resolved_reviews(review_conn, tenant_id=tenant_id, status=status)
    elif status == "all":
        # status=None 让 list_resolved_reviews/count_resolved_reviews 同时
        # 统计 approved+rejected；路由层用 "all" 这个显式值表达"不筛选"，
        # 不直接暴露 None 给客户端。
        reviews = await list_resolved_reviews(
            review_conn, tenant_id=tenant_id, status=None, limit=page_size, offset=offset
        )
        total = await count_resolved_reviews(review_conn, tenant_id=tenant_id, status=None)
    else:
        raise HTTPException(status_code=400, detail="status 必须是 pending/approved/rejected/all")
    return ReviewListResponse(reviews=reviews, total=total)


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
            now=datetime.now(),
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

（`approve`/`reject` 两个端点本身没有改动，一并列出是因为整个文件被替换。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_graph_review_routes.py -q`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_graph_review_routes.py tests/api/test_admin_graph_review_routes.py
git commit -m "feat(api): expose page/page_size params and total count on graph review list endpoint"
```

---

## Task 3: 前端分页器组件

**Files:**
- Create: `frontend/src/admin/pagination.ts`
- Create: `frontend/src/admin/Pager.tsx`

**Interfaces:**
- Produces:
  - `getPageNumbers(current: number, total: number): (number | 'ellipsis')[]`（`pagination.ts`）
  - `Pager` 组件，props `{ page: number; totalPages: number; onPageChange: (page: number) => void }`（`Pager.tsx`），`totalPages <= 1` 时渲染 `null`

这个任务产出的组件在这一步还没有被任何页面引用，项目没有配置前端测试框架，所以这一步只用类型检查验证，真正的行为验证放在 Task 4（组件被接进 `GraphReviewsPage.tsx` 之后）一起做。

- [ ] **Step 1: 创建 `frontend/src/admin/pagination.ts`**

```ts
export type PageToken = number | 'ellipsis'

/**
 * 生成分页器要渲染的页码序列：总页数 <= 7 时全部列出；超过 7 页时固定
 * 展示首页、尾页、当前页前后各 1 页，中间用 'ellipsis' 断开——常见的
 * "1 2 ... 5 6 7 ... 20" 分页器样式，避免页数很多时把所有页码平铺出来。
 */
export function getPageNumbers(current: number, total: number): PageToken[] {
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1)
  }
  const pages: PageToken[] = [1]
  if (current > 3) {
    pages.push('ellipsis')
  }
  const start = Math.max(2, current - 1)
  const end = Math.min(total - 1, current + 1)
  for (let page = start; page <= end; page += 1) {
    pages.push(page)
  }
  if (current < total - 2) {
    pages.push('ellipsis')
  }
  pages.push(total)
  return pages
}
```

- [ ] **Step 2: 创建 `frontend/src/admin/Pager.tsx`**

```tsx
import { getPageNumbers } from './pagination'

interface PagerProps {
  page: number
  totalPages: number
  onPageChange: (page: number) => void
}

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const pageButtonClass = (active: boolean) =>
  `min-h-[36px] min-w-[36px] cursor-pointer border-2 border-ink px-2 text-sm font-bold transition ${focusRing} ${
    active ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink hover:bg-card'
  }`

export function Pager({ page, totalPages, onPageChange }: PagerProps) {
  if (totalPages <= 1) return null

  return (
    <nav aria-label="分页" className="flex items-center gap-1.5">
      <button
        type="button"
        onClick={() => onPageChange(page - 1)}
        disabled={page <= 1}
        aria-label="上一页"
        className={`${pageButtonClass(false)} disabled:cursor-not-allowed disabled:opacity-50`}
      >
        ‹
      </button>
      {getPageNumbers(page, totalPages).map((token, index) =>
        token === 'ellipsis' ? (
          <span key={`ellipsis-${index}`} className="px-1 text-ink-soft">
            …
          </span>
        ) : (
          <button
            key={token}
            type="button"
            onClick={() => onPageChange(token)}
            aria-current={token === page ? 'page' : undefined}
            className={pageButtonClass(token === page)}
          >
            {token}
          </button>
        ),
      )}
      <button
        type="button"
        onClick={() => onPageChange(page + 1)}
        disabled={page >= totalPages}
        aria-label="下一页"
        className={`${pageButtonClass(false)} disabled:cursor-not-allowed disabled:opacity-50`}
      >
        ›
      </button>
    </nav>
  )
}
```

- [ ] **Step 3: 类型检查**

Run（在 `frontend/` 目录下）: `npm run typecheck`
Expected: 无错误退出。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/admin/pagination.ts frontend/src/admin/Pager.tsx
git commit -m "feat(admin): add reusable Pager component"
```

---

## Task 4: 把分页接入知识图谱审核页面

**Files:**
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`

**Interfaces:**
- Consumes: Task 2 的 `GET /api/admin/graph-reviews` 响应新增的 `total` 字段；Task 3 的 `Pager` 组件

- [ ] **Step 1: 加 import 和页大小常量**

把文件顶部（第 1-4 行）：

```tsx
import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
```

改成：

```tsx
import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { Pager } from './Pager'

const PAGE_SIZE = 20
```

- [ ] **Step 2: 加分页相关的 state**

在 `const [processingId, setProcessingId] = useState<number | null>(null)`（第 47 行）之后追加：

```tsx
  const [pendingPage, setPendingPage] = useState(1)
  const [pendingTotal, setPendingTotal] = useState(0)
  const [historyPage, setHistoryPage] = useState(1)
  const [historyTotal, setHistoryTotal] = useState(0)
```

- [ ] **Step 3: 切换历史筛选条件时把历史页码重置回第一页**

把（第 55-57 行）：

```tsx
  useEffect(() => {
    setHistoryLoaded(false)
  }, [historyFilter])
```

改成：

```tsx
  useEffect(() => {
    setHistoryLoaded(false)
    setHistoryPage(1)
  }, [historyFilter])
```

- [ ] **Step 4: 处理"当前页被清空"的情况**

批准/驳回会让当前页的数据变少；如果恰好把当前页最后一条处理掉，用户会停在一个 offset 超出范围、看起来"没有记录"但实际上前面还有数据的空页。在 Task 4 Step 3 修改过的 `useEffect`（现在是第 55-58 行）之后新增两个 effect：

```tsx
  // 批准/驳回把当前页清空后（比如最后一页只剩一条、处理完变空），自动
  // 退回上一页，而不是让用户停留在一个明明还有数据、只是 offset 超出
  // 范围而显示"没有记录"的空页面。
  useEffect(() => {
    if (pendingLoaded && pending.length === 0 && pendingPage > 1) {
      setPendingPage((page) => page - 1)
    }
  }, [pendingLoaded, pending.length, pendingPage])

  useEffect(() => {
    if (historyLoaded && history.length === 0 && historyPage > 1) {
      setHistoryPage((page) => page - 1)
    }
  }, [historyLoaded, history.length, historyPage])
```

- [ ] **Step 5: `refreshPending` 带上分页参数**

把 `refreshPending`（当前第 59-88 行）整个替换成：

```tsx
  const refreshPending = useCallback(async () => {
    if (!sessionToken) return
    try {
      const response = await adminFetch(
        `/api/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=pending&page=${pendingPage}&page_size=${PAGE_SIZE}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载待审核列表失败'))
      }
      const data = (await response.json()) as { reviews: PendingReview[]; total: number }
      setPending(data.reviews)
      setPendingTotal(data.total)
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
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载待审核列表失败')
    } finally {
      setPendingLoaded(true)
    }
  }, [sessionToken, tenantId, pendingPage])
```

- [ ] **Step 6: `refreshHistory` 带上分页参数**

把 `refreshHistory`（当前第 90-108 行）整个替换成：

```tsx
  const refreshHistory = useCallback(async () => {
    if (!sessionToken) return
    try {
      const response = await adminFetch(
        `/api/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=${historyFilter}&page=${historyPage}&page_size=${PAGE_SIZE}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载历史记录失败'))
      }
      const data = (await response.json()) as { reviews: ResolvedReview[]; total: number }
      setHistory(data.reviews)
      setHistoryTotal(data.total)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载历史记录失败')
    } finally {
      setHistoryLoaded(true)
    }
  }, [sessionToken, tenantId, historyFilter, historyPage])
```

（`refreshPending`/`refreshHistory` 的依赖数组分别多了 `pendingPage`/`historyPage`——两个函数的 `useCallback` 引用会在对应页码变化时更新，第 110-121 行现有的"切 tab 时触发对应 refresh"的 `useEffect` 不用改，它已经把这两个函数列在依赖数组里，页码一变、函数引用一变，effect 自然会重新跑一次。）

- [ ] **Step 7: 渲染 Pager**

把待审核列表的空状态提示（当前第 294-296 行）：

```tsx
      {tab === 'pending' && pendingLoaded && pending.length === 0 && (
        <p className="text-ink-soft">当前没有待审核的候选关系。</p>
      )}
```

改成：

```tsx
      {tab === 'pending' && pendingLoaded && pending.length === 0 && (
        <p className="text-ink-soft">当前没有待审核的候选关系。</p>
      )}
      {tab === 'pending' && pendingLoaded && pending.length > 0 && (
        <Pager
          page={pendingPage}
          totalPages={Math.max(1, Math.ceil(pendingTotal / PAGE_SIZE))}
          onPageChange={setPendingPage}
        />
      )}
```

把历史记录列表的空状态提示（当前第 334-336 行）：

```tsx
      {tab === 'history' && historyLoaded && history.length === 0 && (
        <p className="text-ink-soft">还没有处理过的记录。</p>
      )}
```

改成：

```tsx
      {tab === 'history' && historyLoaded && history.length === 0 && (
        <p className="text-ink-soft">还没有处理过的记录。</p>
      )}
      {tab === 'history' && historyLoaded && history.length > 0 && (
        <Pager
          page={historyPage}
          totalPages={Math.max(1, Math.ceil(historyTotal / PAGE_SIZE))}
          onPageChange={setHistoryPage}
        />
      )}
```

- [ ] **Step 8: 类型检查 + 构建**

Run（在 `frontend/` 目录下）:
```bash
npm run typecheck
npm run build
```
Expected: 两条命令都无错误退出。

- [ ] **Step 9: 手动验证**

项目没有配置浏览器自动化/前端测试框架，这一步需要人工（或执行计划时环境里可用的浏览器工具）核对，按顺序检查：

1. 用 `scripts/start-backend.ps1` + `scripts/start-frontend.ps1` 启动前后端（或已经在跑就跳过），登录管理后台，进入"知识图谱审核"页面。
2. 造出超过 20 条待审核记录（可以用 `python -m app.graphrag.review_cli` 或直接摄取几篇带术语表外实体的文档触发抽取），确认"待审核" tab 下方出现分页器，页码正确、点击"下一页"/具体页码能看到不同的记录。
3. 切到"历史记录" tab，确认同样出现分页器；切换"全部/已批准/已驳回"筛选按钮时，页码应该自动回到第 1 页（不是停留在切换前的页码）。
4. 在待审核列表翻到最后一页且该页只有 1 条记录时，批准或驳回这条记录，确认页面自动退回上一页，而不是停留在一个空白页。
5. 打开浏览器开发者工具的 Network 面板，确认翻页时请求的 URL 带上了正确的 `page`/`page_size` 参数。

- [ ] **Step 10: 提交**

```bash
git add frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(admin): wire pagination into graph review pending/history tabs"
```

---

## Task 5: 关系抽取 prompt 补实体范围约束

**Files:**
- Modify: `app/graphrag/llm_extractor.py:16-34`（`_SYSTEM_PROMPT`）
- Test: `tests/graphrag/test_llm_extractor.py`

**Interfaces:** 无——这是独立的 prompt 文本改动，不涉及函数签名变化，不被其它任务消费。

这一步只能测"prompt 文本里确实包含了这段范围约束"，测不出"LLM 实际抽取效果有没有变好"——那需要真实文档 + 真实 LLM 调用才能验证，不在这份计划的范围内（现有 `terminology_seed.yaml` 还是占位数据，没有真实业务文档可用来跑端到端效果对比）。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_llm_extractor.py` 末尾追加：

```python
async def test_system_prompt_requires_concrete_entities_and_excludes_generic_category_words():
    provider = SpyLLMProvider('{"relations": []}')

    await extract_candidate_relations(
        ["片段"],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    system_message = provider.received_requests[0].messages[0]["content"]
    assert "专有名词指具体、可命名的业务实体" in system_message
    for excluded_word in ["设备", "问题", "服务", "顾客", "流程"]:
        assert excluded_word in system_message
    assert "不算专有名词" in system_message
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_llm_extractor.py -q`
Expected: FAIL，`assert "专有名词指具体、可命名的业务实体" in system_message` 断言失败（现有 prompt 里没有这句话）。

- [ ] **Step 3: 实现**

把 `app/graphrag/llm_extractor.py` 里的 `_SYSTEM_PROMPT`（当前第 16-34 行）整个替换成：

```python
_SYSTEM_PROMPT = (
    "你是知识图谱关系抽取器。"
    "请从给定文档片段中抽取专有名词之间的关系。"
    '专有名词指具体、可命名的业务实体：产品/型号名（如"大床房"）、编号'
    '（如"302号房"）、地点名（如"三楼健身房"）、机构/品牌名（如"某连锁'
    '酒店"）、职务/角色头衔（如"值班经理"）等；不要把通用类别词、动作或'
    '状态描述当成实体抽取，例如"设备""问题""服务""顾客""流程"这类没有'
    '具体指代对象的泛称，哪怕在文档里反复出现也不算专有名词。'
    '只输出 JSON：{"relations":[{"subject":"...","object":"...","relation_type":"RELATED_TO"}]}。'
    "relation_type 仅允许以下 10 种，每种给一个例子帮助理解：\n"
    'RELATED_TO（兜底弱关联，如"促销活动 RELATED_TO 会员日"）、\n'
    'PART_OF（部分-整体，如"客房 PART_OF 酒店"）、\n'
    'IS_A（类别从属，如"大床房 IS_A 客房"）、\n'
    'REQUIRES（前提依赖，如"预订套餐 REQUIRES 会员资格"）、\n'
    'ALTERNATIVE_TO（替代/类似，如"标准间 ALTERNATIVE_TO 大床房"）、\n'
    'CAUSES（因果，如"恶劣天气 CAUSES 接送延误"）、\n'
    'ADDRESSED_BY（问题由方案解决，如"房间异味 ADDRESSED_BY 更换房间"）、\n'
    'LOCATED_IN（空间/组织归属，如"健身房 LOCATED_IN 三楼"）、\n'
    'APPLIES_TO（适用范围，如"会员折扣 APPLIES_TO 非节假日预订"）、\n'
    'PRECEDES（流程先后，如"入住登记 PRECEDES 领取房卡"）。\n'
    "不确定的内容不要编造，抽不出关系就返回空列表。"
    "如果输入包含多个用 [片段N] 标记分隔的片段，只抽取同一个片段内部出现的"
    "关系，不要把不同片段里的实体强行关联起来。"
)
```

（只在开头插入了一段实体范围约束，relation_type 那一段 10 个类别的文字原样保留、一个字不改。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_llm_extractor.py -q`
Expected: PASS，全部用例（包括改动前就有的用例）通过——特别注意确认 `test_system_prompt_lists_all_ten_relation_types_and_forbids_cross_segment_relations` 这条已有测试也仍然通过（它断言 10 种 relation_type 和"不要把不同片段里的实体强行关联起来"这句话还在 prompt 里）。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/llm_extractor.py tests/graphrag/test_llm_extractor.py
git commit -m "feat(graphrag): scope entity extraction to concrete nameable entities"
```

---

## 全部任务完成后

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了已知无关的 `test_returns_none_when_tts_not_configured` 之外全部通过。
