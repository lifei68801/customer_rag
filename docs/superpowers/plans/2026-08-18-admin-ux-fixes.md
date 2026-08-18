# 管理后台 UI/UX 审查修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复前端 UI/UX 审查中发现的问题：4 项低风险速修、一套完整的租户注册表（后端新表+校验+API，前端动态租户切换器）、术语库类型字段改为下拉选择、文档/术语库补充分页。

**Architecture:** 后端新增 `app/graphrag/tenants_store.py`（租户注册表 CRUD + 存量数据迁移回填）和 `app/api/admin_tenant_routes.py`（租户管理 API），在 5 个既有管理路由文件的写接口里接入租户存在性校验；`terms_store.py`/`ingestion/tracking.py` 加可选分页参数（默认行为不变，不影响现有非分页调用方）。前端复用已有的 `Pager.tsx`/`GraphReviewsPage.tsx` 分页模式，`TenantSwitcher.tsx` 改造成动态拉取+内联新建。

**Tech Stack:** FastAPI + aiosqlite（后端），React + TypeScript + Tailwind（前端）。

**Spec:** docs/superpowers/specs/2026-08-18-admin-ux-fixes-design.md

## Global Constraints

- `list_terms()`（`app/graphrag/terms_store.py`）和 `list_tracked_files()`（`app/ingestion/tracking.py`）被 agent 检索/ingestion 流水线/eval runner/CLI 等大量非分页场景调用，新增分页参数必须是可选的、默认值保持现有全量行为，不能修改任何现有调用点。
- 租户存在性校验（`require_active_tenant`）只接入 5 个管理后台路由文件（`admin_document_routes.py`/`admin_graph_review_routes.py`/`admin_ontology_routes.py`/`admin_schema_etl_routes.py`/`admin_terms_routes.py`）的写接口（POST/PUT/DELETE），不接入聊天运行时路径（`agent_routes.py`/`qa_routes.py`/`session_routes.py`/`voice_routes.py`），也不接入这 5 个文件里的 GET 读接口。
- `product-lines` 相关接口（`/api/admin/ontology/product-lines...`）不带 `tenant_id`（全局配置），不需要租户校验。
- 每个新写的后端函数/路由都要有对应的 pytest 覆盖；每个前端改动都要过 `npx tsc --noEmit`。
- 提交信息、代码风格跟随本仓库既有约定（中文注释解释"为什么"，不解释"是什么"）。

---

### Task 1: 低风险速修（纯前端，零依赖）

**Files:**
- Modify: `frontend/tailwind.config.ts`
- Modify: `frontend/src/components/ChatSidebar.tsx`
- Modify: `frontend/src/admin/AdminLayout.tsx`
- Modify: `frontend/index.html`
- Modify: `frontend/src/components/Hero.tsx`
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`
- Test: 无需新增自动化测试（纯样式/文案/顺序调整），用 `npx tsc --noEmit` 做类型检查兜底。

**Interfaces:** 无（不改变任何组件的 props/导出签名）。

- [ ] **Step 1: 修复 `status.error` 对比度**

`frontend/tailwind.config.ts` 里把：

```ts
        status: {
          success: '#A9D877',
          error: '#F97264',
        },
```

改成：

```ts
        status: {
          success: '#A9D877',
          error: '#DC2626',
        },
```

（`#F97264` 在白色/`paper` 背景上对比度约 2.75:1，低于 WCAG AA 正文 4.5:1 门槛；`#DC2626` 即 Tailwind red-600，对白色背景对比度约 4.83:1，通过 AA。这是唯一色值定义，改这一处，`OntologySchemaPage.tsx` 里全部 `text-status-error`/`border-status-error` 用法自动生效。）

- [ ] **Step 2: 统一 `ChatSidebar.tsx` 的错误色**

`frontend/src/components/ChatSidebar.tsx` 里有两处：

```tsx
          <p className="p-2 text-sm text-red-700">会话列表加载失败：{sessionsError}</p>
```
```tsx
        {deleteError && <p className="p-2 text-sm text-red-700">{deleteError}</p>}
```

都把 `text-red-700` 改成 `text-status-error`，让全站错误文字色只有一个来源（`tailwind.config.ts` 的 `status.error`）。

- [ ] **Step 3: 重排后台侧边栏导航顺序**

`frontend/src/admin/AdminLayout.tsx` 里 `<nav>` 内 5 个 `<NavLink>` 当前顺序是 文档管理→知识图谱审核→术语库管理→ETL 跑批→本体 Schema 管理。改成：本体 Schema 管理→文档管理→知识图谱审核→ETL 跑批→术语库管理。即把"本体 Schema 管理"那个 `<NavLink>` 整个移到最前面，"术语库管理"那个移到最后面，中间"文档管理→知识图谱审核→ETL 跑批"三个相对顺序不变：

```tsx
          <nav className="flex flex-row flex-wrap gap-2 md:flex-col">
            <NavLink to="/admin/ontology" className={navLinkClass}>
              本体 Schema 管理
            </NavLink>
            <NavLink to="/admin/documents" className={navLinkClass}>
              文档管理
            </NavLink>
            <NavLink to="/admin/graph-reviews" className={navLinkClass}>
              知识图谱审核
            </NavLink>
            <NavLink to="/admin/schema-etl" className={navLinkClass}>
              ETL 跑批
            </NavLink>
            <NavLink to="/admin/terms" className={navLinkClass}>
              术语库管理
            </NavLink>
          </nav>
```

（`App.tsx` 的路由定义和 `index` 的默认跳转目标 `documents` 不用改——这里只调整侧边栏链接的展示顺序，不改路由本身。）

- [ ] **Step 4: 统一产品名称**

`frontend/index.html` 里：

```html
    <title>客服智能问答 Demo</title>
```

改成：

```html
    <title>企业数字员工</title>
```

`frontend/src/components/Hero.tsx` 整个文件改成：

```tsx
export function Hero() {
  return (
    <header className="border-b-2 border-ink px-6 py-10 text-center">
      <h1 className="text-3xl font-bold text-ink sm:text-4xl">
        企业数字员工
      </h1>
      <p className="mx-auto mt-3 max-w-xl text-ink-soft">
        随时待命的Know Know · 嗨，轻轻一敲，告诉我你想了解什么？
      </p>
    </header>
  )
}
```

（标题统一成"企业数字员工"，跟导航栏/Footer 一致；原来的"随时待命的Know Know"降级成副标题的一部分，不整句删除。）

- [ ] **Step 5: 清理无效的 `aria-disabled`**

`frontend/src/admin/SchemaEtlPage.tsx` 里：

```tsx
      <form
        onSubmit={handleUpload}
        aria-disabled={confirmed !== true}
        className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
      >
```

删掉 `aria-disabled={confirmed !== true}` 这一行，改成：

```tsx
      <form
        onSubmit={handleUpload}
        className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
      >
```

（`<form>` 不是可交互 widget role，`aria-disabled` 在它身上没有实际语义；表单的禁用行为完全靠内部 `<input>`/`<button>` 各自的 `disabled` 属性实现，删掉这行不改变任何行为。）

- [ ] **Step 6: 验证**

```bash
cd frontend && npx tsc --noEmit
```
Expected: 无报错。

- [ ] **Step 7: Commit**

```bash
git add frontend/tailwind.config.ts frontend/src/components/ChatSidebar.tsx frontend/src/admin/AdminLayout.tsx frontend/index.html frontend/src/components/Hero.tsx frontend/src/admin/SchemaEtlPage.tsx
git commit -m "fix(frontend): correct error color contrast, nav order, product naming, and stray aria-disabled"
```

---

### Task 2: 租户注册表存储层

**Files:**
- Create: `app/graphrag/tenants_store.py`
- Modify: `app/api/deps.py`
- Test: `tests/graphrag/test_tenants_store.py`

**Interfaces:**
- Produces（后续任务依赖这些确切签名）：
  - `class TenantNotFoundError(Exception)`
  - `class TenantAlreadyExistsError(Exception)`
  - `async def create_tenants_table(conn: aiosqlite.Connection) -> None`（只建表，不做迁移回填——供测试 fixture 在只有单张表的最小 `:memory:` 连接上使用，不需要传入 `ingestion_conn`）
  - `async def ensure_tenants_schema(review_conn: aiosqlite.Connection, ingestion_conn: aiosqlite.Connection) -> None`（建表 + 迁移回填，真实生产路径用这个）
  - `async def list_tenants(conn: aiosqlite.Connection, *, include_disabled: bool = False) -> list[dict]`（每个 dict 形如 `{"tenant_id": str, "name": str, "status": str}`）
  - `async def create_tenant(conn: aiosqlite.Connection, *, tenant_id: str, name: str) -> None`
  - `async def require_active_tenant(conn: aiosqlite.Connection, tenant_id: str) -> None`（不存在或非 active 都抛 `TenantNotFoundError`）
  - `async def set_tenant_status(conn: aiosqlite.Connection, tenant_id: str, status: str) -> None`（`status` 只接受 `"active"`/`"disabled"`，租户不存在抛 `TenantNotFoundError`）

- [ ] **Step 1: 建表 + 迁移回填逻辑**

创建 `app/graphrag/tenants_store.py`：

```python
from __future__ import annotations

import aiosqlite

__all__ = [
    "TenantAlreadyExistsError",
    "TenantNotFoundError",
    "create_tenants_table",
    "ensure_tenants_schema",
    "list_tenants",
    "create_tenant",
    "require_active_tenant",
    "set_tenant_status",
]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_VALID_STATUSES = ("active", "disabled")


class TenantNotFoundError(Exception):
    """指定的 tenant_id 不存在于注册表，或存在但状态不是 active。"""


class TenantAlreadyExistsError(Exception):
    """提交的 tenant_id 已经在注册表里。"""


async def _discover_historical_tenant_ids(
    review_conn: aiosqlite.Connection, ingestion_conn: aiosqlite.Connection
) -> set[str]:
    """上线校验前，从两个库里已有的租户作用域表各自 UNION 出历史出现过的
    tenant_id——两个库是不同的 SQLite 文件（review_conn 是
    graph_review_db_path，ingestion_conn 是 ingestion_db_path），aiosqlite
    的两个连接不能直接跨库 UNION，只能各查各的再在 Python 里合并。
    """
    found: set[str] = set()
    for table in ("terms", "ontology_term_types", "etl_runs", "graph_review_queue"):
        cursor = await review_conn.execute(f"SELECT DISTINCT tenant_id FROM {table}")
        rows = await cursor.fetchall()
        found.update(row[0] for row in rows if row[0])
    cursor = await ingestion_conn.execute("SELECT DISTINCT tenant_id FROM ingested_documents")
    rows = await cursor.fetchall()
    found.update(row[0] for row in rows if row[0])
    return found


async def create_tenants_table(conn: aiosqlite.Connection) -> None:
    """只建表，不做迁移回填。真实生产路径不用这个（见 ensure_tenants_schema），
    这个函数是给测试 fixture 用的——很多既有测试的 conn fixture 只建了单张
    表（比如 test_admin_terms_routes.py 的 terms_conn 只建 terms 表），如果
    直接调用 ensure_tenants_schema() 会因为 _discover_historical_tenant_ids()
    要查的 ontology_term_types/etl_runs/graph_review_queue 等表在这个最小
    fixture 里根本不存在而报 "no such table"。
    """
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def ensure_tenants_schema(
    review_conn: aiosqlite.Connection, ingestion_conn: aiosqlite.Connection
) -> None:
    """建表 + 存量数据一次性回填。全程幂等：CREATE TABLE IF NOT EXISTS +
    INSERT OR IGNORE，重复调用（每次进程启动都会走一遍）不会报错也不会
    覆盖已经存在的注册记录（比如后台手动改过的 name/status）。
    """
    await create_tenants_table(review_conn)
    historical_ids = await _discover_historical_tenant_ids(review_conn, ingestion_conn)
    # 全新环境没有任何历史数据时，至少保证 'demo' 存在——这是本仓库其它地方
    # （比如 TenantContext.tsx 的 sessionStorage 默认值）一直假设存在的
    # 兜底租户，注册表上线不能让这个既有默认体验失效。
    if not historical_ids:
        historical_ids.add("demo")
    for tenant_id in historical_ids:
        await review_conn.execute(
            "INSERT OR IGNORE INTO tenants (tenant_id, name, status) VALUES (?, ?, 'active')",
            (tenant_id, tenant_id),
        )
    await review_conn.commit()


async def list_tenants(
    conn: aiosqlite.Connection, *, include_disabled: bool = False
) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    if include_disabled:
        cursor = await conn.execute(
            "SELECT tenant_id, name, status FROM tenants ORDER BY tenant_id"
        )
    else:
        cursor = await conn.execute(
            "SELECT tenant_id, name, status FROM tenants WHERE status = 'active' ORDER BY tenant_id"
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def create_tenant(conn: aiosqlite.Connection, *, tenant_id: str, name: str) -> None:
    cursor = await conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,))
    if await cursor.fetchone() is not None:
        raise TenantAlreadyExistsError(f"租户 {tenant_id!r} 已存在")
    await conn.execute(
        "INSERT INTO tenants (tenant_id, name, status) VALUES (?, ?, 'active')",
        (tenant_id, name),
    )
    await conn.commit()


async def require_active_tenant(conn: aiosqlite.Connection, tenant_id: str) -> None:
    cursor = await conn.execute("SELECT status FROM tenants WHERE tenant_id = ?", (tenant_id,))
    row = await cursor.fetchone()
    if row is None or row[0] != "active":
        raise TenantNotFoundError(f"租户 {tenant_id!r} 不存在或未启用")


async def set_tenant_status(conn: aiosqlite.Connection, tenant_id: str, status: str) -> None:
    if status not in _VALID_STATUSES:
        raise ValueError(f"非法 status: {status!r}")
    cursor = await conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,))
    if await cursor.fetchone() is None:
        raise TenantNotFoundError(f"租户 {tenant_id!r} 不存在")
    await conn.execute("UPDATE tenants SET status = ? WHERE tenant_id = ?", (status, tenant_id))
    await conn.commit()
```

- [ ] **Step 2: 接入 `deps.py` 的 `get_review_conn`**

`app/api/deps.py` 顶部 import 区加一行（放在现有 `from app.graphrag.etl_runs_store import ensure_etl_runs_schema` 后面）：

```python
from app.graphrag.tenants_store import ensure_tenants_schema
```

`get_review_conn` 函数体里，`await ensure_etl_runs_schema(conn)` 那一行之后加：

```python
                    # tenants 注册表的迁移回填需要同时读 review_conn 和
                    # ingestion_conn 两个库里的历史 tenant_id（见
                    # tenants_store.py::_discover_historical_tenant_ids 的
                    # 说明），这里显式拿一次 ingestion_conn——get_ingestion_conn
                    # 自己是懒加载单例，这次调用要么复用已经开着的连接，要么
                    # 顺带把它开起来，不会重复建库/重复迁移。
                    ingestion_conn = await get_ingestion_conn(settings)
                    await ensure_tenants_schema(conn, ingestion_conn)
```

（插入位置在 `try:` 块内，`await ensure_etl_runs_schema(conn)` 之后、`except Exception:` 之前——`get_review_conn` 函数体的具体样子已经在当前文件里，照现有缩进插入即可。）

- [ ] **Step 3: 单元测试**

创建 `tests/graphrag/test_tenants_store.py`：

```python
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.tenants_store import (
    TenantAlreadyExistsError,
    TenantNotFoundError,
    create_tenant,
    ensure_tenants_schema,
    list_tenants,
    require_active_tenant,
    set_tenant_status,
)
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.graphrag.etl_runs_store import ensure_etl_runs_schema
from app.graphrag.review_queue import ensure_review_schema
from app.ingestion.tracking import ensure_tracking_schema, record_ingested

pytestmark = pytest.mark.anyio


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_etl_runs_schema(conn)
    return conn


async def _ingestion_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
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
```

- [ ] **Step 4: 跑测试**

```bash
python -m pytest tests/graphrag/test_tenants_store.py -q
```
Expected: 全部通过。

- [ ] **Step 5: Commit**

```bash
git add app/graphrag/tenants_store.py app/api/deps.py tests/graphrag/test_tenants_store.py
git commit -m "feat(graphrag): add tenants registry store with historical-data backfill"
```

---

### Task 3: 租户管理 API

**Files:**
- Create: `app/api/admin_tenant_routes.py`
- Modify: `app/main.py`
- Test: `tests/api/test_admin_tenant_routes.py`

**Interfaces:**
- Consumes：Task 2 产出的 `tenants_store.py`（`TenantAlreadyExistsError`、`TenantNotFoundError`、`list_tenants`、`create_tenant`、`set_tenant_status`）、`app.tenancy.is_valid_tenant_id`、`app.api.deps.get_review_conn`、`app.api.deps.require_admin_session`。
- Produces：
  - `GET /api/admin/tenants` → `{"tenants": [{"tenant_id": str, "name": str, "status": str}]}`（默认只返回 active）
  - `POST /api/admin/tenants` body `{"tenant_id": str, "name": str}` → 201，返回创建的租户
  - `POST /api/admin/tenants/{tenant_id}/disable` → `{"status": "disabled"}`
  - `POST /api/admin/tenants/{tenant_id}/enable` → `{"status": "active"}`

- [ ] **Step 1: 新建路由文件**

创建 `app/api/admin_tenant_routes.py`（风格参照 `app/api/admin_terms_routes.py`）：

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import aiosqlite

from app.api import deps
from app.tenancy import is_valid_tenant_id
from app.graphrag.tenants_store import (
    TenantAlreadyExistsError,
    TenantNotFoundError,
    create_tenant,
    list_tenants,
    set_tenant_status,
)

router = APIRouter(prefix="/api/admin/tenants", dependencies=[Depends(deps.require_admin_session)])


class TenantResponse(BaseModel):
    tenant_id: str
    name: str
    status: str


class TenantListResponse(BaseModel):
    tenants: list[TenantResponse]


class TenantCreateRequest(BaseModel):
    tenant_id: str
    name: str

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        if not is_valid_tenant_id(value):
            raise ValueError("tenant_id 只能包含字母、数字、下划线和连字符")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name 不能为空")
        return stripped


@router.get("", response_model=TenantListResponse)
async def list_all_tenants(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TenantListResponse:
    tenants = await list_tenants(review_conn)
    return TenantListResponse(tenants=[TenantResponse(**t) for t in tenants])


@router.post("", response_model=TenantResponse)
async def create_new_tenant(
    payload: TenantCreateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TenantResponse:
    try:
        await create_tenant(review_conn, tenant_id=payload.tenant_id, name=payload.name)
    except TenantAlreadyExistsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TenantResponse(tenant_id=payload.tenant_id, name=payload.name, status="active")


@router.post("/{tenant_id}/disable")
async def disable_tenant(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    try:
        await set_tenant_status(review_conn, tenant_id, "disabled")
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在")
    return {"status": "disabled"}


@router.post("/{tenant_id}/enable")
async def enable_tenant(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, str]:
    try:
        await set_tenant_status(review_conn, tenant_id, "active")
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在")
    return {"status": "active"}
```

- [ ] **Step 2: 注册路由**

`app/main.py` 里 import 区加一行（跟其它 `admin_*_router` import 放一起）：

```python
from app.api.admin_tenant_routes import router as admin_tenant_router
```

`app.include_router(admin_schema_etl_router)` 那一行之后加：

```python
app.include_router(admin_tenant_router)
```

- [ ] **Step 3: 测试**

创建 `tests/api/test_admin_tenant_routes.py`（参照 `tests/api/test_admin_ontology_routes.py` 的 fixture 风格：起一个带 `admin_session` override 的 `TestClient`）。测试至少覆盖：
- `GET /api/admin/tenants` 默认只返回 active 租户（种一个 disabled 的，确认不在返回列表里）。
- `POST /api/admin/tenants` 新建成功后能在 `GET` 结果里看到。
- `POST /api/admin/tenants` 提交重复 `tenant_id` 返回 400。
- `POST /api/admin/tenants` 提交非法字符的 `tenant_id`（比如带 `/`）返回 422（Pydantic 校验失败）。
- `POST /api/admin/tenants/{id}/disable` 后该租户从默认 `GET` 结果里消失，`?include_disabled` 相关逻辑如果 `list_tenants` 没暴露这个 query 参数就不用测（本任务的 `GET` 路由没有暴露 `include_disabled` 给前端，只在 store 层留了这个能力）。
- `POST /api/admin/tenants/{id}/enable` 能把上一步禁用的租户重新变回 active。
- 对不存在的 `tenant_id` 调用 `disable`/`enable` 返回 404。

具体 fixture 写法照抄 `tests/api/test_admin_ontology_routes.py` 顶部的 `client`/`review_conn` fixture 模式。

- [ ] **Step 4: 跑测试**

```bash
python -m pytest tests/api/test_admin_tenant_routes.py -q
```
Expected: 全部通过。

- [ ] **Step 5: Commit**

```bash
git add app/api/admin_tenant_routes.py app/main.py tests/api/test_admin_tenant_routes.py
git commit -m "feat(api): add tenant list/create/disable/enable admin routes"
```

---

### Task 4: 5 个既有管理路由接入租户存在性校验

**Files:**
- Modify: `app/api/admin_document_routes.py`
- Modify: `app/api/admin_graph_review_routes.py`
- Modify: `app/api/admin_ontology_routes.py`
- Modify: `app/api/admin_schema_etl_routes.py`
- Modify: `app/api/admin_terms_routes.py`
- Test: 在对应的 `tests/api/test_admin_*.py` 里各加至少一个"未知/禁用租户 → 404"的用例。

**Interfaces:**
- Consumes：Task 2 的 `TenantNotFoundError`、`require_active_tenant`（`from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant`）。

**统一模式**：每个写接口（POST/PUT/DELETE）在现有的 `tenant_id` 格式校验之后（如果该接口本来就没有格式校验，就放在函数体最前面、拿到 `tenant_id` 和 `review_conn` 之后），插入：

```python
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
```

需要逐个改的位置：

- [ ] **Step 1: `app/api/admin_document_routes.py`**

  文件顶部加 `from app.graphrag.tenants_store import TenantNotFoundError, require_active_tenant`。

  4 处插入点（`_validate_tenant_id(tenant_id)` 那一行之后）：
  - `upload_document`（第 196 行 `_validate_tenant_id(tenant_id)` 之后；此函数已有 `review_conn` 参数）
  - `delete_document`（第 289 行附近；已有 `review_conn` 参数）
  - `retry_ingestion_job`（第 334 行附近；已有 `review_conn` 参数）
  - `delete_ingestion_job`（第 373 行附近；已有 `review_conn` 参数）

  （这四个函数的具体行号以改动前重新 `grep -n "_validate_tenant_id" app/api/admin_document_routes.py` 为准，逐个确认每处都已经有 `review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)` 参数——目前四个都有，不需要额外加依赖注入。）

- [ ] **Step 2: `app/api/admin_graph_review_routes.py`**

  文件顶部加同样的 import。

  `approve` 和 `reject` 两个函数的 `tenant_id` 来自 `payload.tenant_id`（不是路径参数），插入点在函数体里拿到 `payload` 之后、执行任何写操作之前：

  ```python
      try:
          await require_active_tenant(review_conn, payload.tenant_id)
      except TenantNotFoundError:
          raise HTTPException(status_code=404, detail="租户不存在或未启用")
  ```

  确认这两个函数已有 `review_conn` 参数（`approve`/`reject` 都需要 `review_conn` 来查/改审核队列，应该已经有）。

- [ ] **Step 3: `app/api/admin_ontology_routes.py`**

  文件顶部加同样的 import。

  以下函数插入校验（全部带 `tenant_id` 路径参数，`product-lines` 相关的几个函数不带 `tenant_id`，跳过）：
  - `create_term_type_category`（POST `/{tenant_id}/term-types`）
  - `update_term_type_category`（PUT `/{tenant_id}/term-types/{value}`）
  - `delete_term_type_category`（DELETE `/{tenant_id}/term-types/{value}`）
  - `create_tenant_relation_type`（POST `/{tenant_id}/relation-types`）
  - `update_tenant_relation_type`（PUT `/{tenant_id}/relation-types/{relation_type}`）
  - `delete_tenant_relation_type`（DELETE `/{tenant_id}/relation-types/{relation_type}`）
  - `add_tenant_constraint`（POST `/{tenant_id}/constraints`）
  - `remove_tenant_constraint`（DELETE `/{tenant_id}/constraints`）
  - `checkout_tenant_ontology_draft`（POST `/{tenant_id}/checkout`）
  - `confirm_tenant_ontology`（POST `/{tenant_id}/confirm`）

  这 10 个函数目前都已有 `review_conn` 参数，直接在函数体最前面插入校验代码块即可。

  **特殊情况 `migrate_tenant_relation_type`**（POST `/{tenant_id}/relation-types/migrate`）：这个函数目前**没有** `review_conn` 参数（只用 `graph_client` 直连 Neo4j，不碰 SQLite）。需要：
  1. 给函数签名加一个新参数 `review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)`（注意此文件顶部要确认已 import `aiosqlite`，应该已经有）。
  2. 在函数体最前面插入同样的校验代码块。

- [ ] **Step 4: `app/api/admin_schema_etl_routes.py`**

  文件顶部加同样的 import。

  `start_schema_etl_run`（POST `/runs`）插入校验。确认该函数已有 `review_conn` 参数（应该有，因为要查 `ontology_confirmed` 状态）。

- [ ] **Step 5: `app/api/admin_terms_routes.py`**

  文件顶部加同样的 import。

  `create_new_term`（POST）、`update_existing_term`（PUT）、`delete_existing_term`（DELETE）三个函数插入校验，都已有 `review_conn` 参数。

- [ ] **Step 6: 补测试**

  在下列既有测试文件里各加一个"未知租户 → 404"的用例（不需要每个写接口都测一遍，每个文件挑 1 个有代表性的写接口验证校验确实生效即可，因为校验逻辑本身已经在 `tests/graphrag/test_tenants_store.py` 里覆盖过）：
  - `tests/api/test_admin_document_routes.py`：对 `upload_document` 或 `delete_document` 用一个不存在的 `tenant_id` 调用，断言 404。
  - `tests/api/test_admin_graph_review_routes.py`：对 `approve` 或 `reject` 用不存在的 `tenant_id` 调用，断言 404。
  - `tests/api/test_admin_ontology_routes.py`：对 `create_term_type_category` 用不存在的 `tenant_id` 调用，断言 404；再对 `migrate_tenant_relation_type` 用不存在的 `tenant_id` 调用一次，确认新加的 `review_conn` 依赖没有破坏这个路由本身能正常工作（比如先用一个真实存在的 active 租户走一遍现有的迁移成功用例，确认没有回归）。
  - `tests/api/test_admin_schema_etl_routes.py`：对 `start_schema_etl_run` 用不存在的 `tenant_id` 调用，断言 404。
  - `tests/api/test_admin_terms_routes.py`：对 `create_new_term` 用不存在的 `tenant_id` 调用，断言 404。

  **这是本任务里风险最高的一步**：这 5 个测试文件的 conn fixture 目前都是通过 `app.dependency_overrides[deps.get_review_conn] = lambda: xxx_conn` 直接注入一个手工建表的 `:memory:` 连接（比如 `test_admin_terms_routes.py` 的 `terms_conn` fixture 只建了 `terms` 表），完全绕过真实的 `deps.get_review_conn()`，也就绕过了 Task 2 里接进 `get_review_conn` 的 `ensure_tenants_schema` 调用——`tenants` 表在这些 fixture 里根本不存在。新加的校验一旦接上，这些文件里**所有**现有的写接口测试用例都会因为 `require_active_tenant` 查询一张不存在的表而报错（不是"查不到租户"的 404，是更底层的 SQL 报错），必须在改动路由代码之前先修 fixture。

  修法（对 5 个文件的 conn fixture 逐一处理）：
  1. `grep -n "tenant_id" tests/api/test_admin_xxx_routes.py` 找出该文件全部测试用例里出现过的 tenant_id 字面量（路径参数、query 参数、请求体字段都要看，比如 `test_admin_ontology_routes.py` 是 `/api/admin/ontology/{tenant_id}/...` 路径形式，`tenant_id` 直接嵌在 URL 字符串里，要连 URL 一起找）。
  2. 在该文件的 conn fixture 函数体里，建表逻辑之后（`terms_conn`/`review_conn`/等 fixture 名以各文件实际的为准）加：
     ```python
     from app.graphrag.tenants_store import create_tenants_table, create_tenant
     await create_tenants_table(conn)
     for _tid in ("t1", "tenant_a", ...):  # 换成 Step 1 grep 出来的实际值
         await create_tenant(conn, tenant_id=_tid, name=_tid)
     ```
     如果一个文件里有多个 conn fixture（比如既有 `terms_conn` 又有别的场景专用 fixture），每个都要加。
  3. 加完之后跑该文件全部用例，逐条看失败信息——如果还有测试因为 `require_active_tenant` 之外的原因把某个 grep 漏掉的 tenant_id 暴露出来（比如测试内联生成的随机 tenant_id、或者写在辅助函数里没被字符串字面量匹配到的），照失败信息补种，直到这个文件全绿。不要用"跳过校验"或者放宽 `require_active_tenant` 语义的方式让测试通过——所有失败都应该通过"在 fixture 里把这个 tenant_id 注册成 active"来解决。

- [ ] **Step 7: 跑测试（全量，确认没有回归）**

```bash
python -m pytest tests/api/test_admin_document_routes.py tests/api/test_admin_graph_review_routes.py tests/api/test_admin_ontology_routes.py tests/api/test_admin_schema_etl_routes.py tests/api/test_admin_terms_routes.py -q
```
Expected: 全部通过，包括新加的 404 用例和所有之前就存在的用例。

- [ ] **Step 8: Commit**

```bash
git add app/api/admin_document_routes.py app/api/admin_graph_review_routes.py app/api/admin_ontology_routes.py app/api/admin_schema_etl_routes.py app/api/admin_terms_routes.py tests/api/test_admin_document_routes.py tests/api/test_admin_graph_review_routes.py tests/api/test_admin_ontology_routes.py tests/api/test_admin_schema_etl_routes.py tests/api/test_admin_terms_routes.py
git commit -m "feat(api): reject writes to unknown or disabled tenants across admin routes"
```

---

### Task 5: `TenantSwitcher.tsx` 动态化 + 内联新建

**Files:**
- Modify: `frontend/src/admin/TenantSwitcher.tsx`
- Test: 无自动化测试文件（本仓库前端目前没有组件测试基础设施），用 `npx tsc --noEmit` + 手动过一遍浏览器验收。

**Interfaces:**
- Consumes：Task 3 的 `GET /api/admin/tenants`、`POST /api/admin/tenants`；`frontend/src/admin/adminApi.ts` 的 `adminFetch`/`extractErrorDetail`；`frontend/src/admin/useAdminAuth.ts` 的 `useAdminAuth`；`frontend/src/admin/TenantContext.tsx` 的 `useAdminTenant`。

- [ ] **Step 1: 改造组件**

`frontend/src/admin/TenantSwitcher.tsx` 整个文件改成：

```tsx
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

interface TenantOption {
  tenant_id: string
  name: string
  status: string
}

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function TenantSwitcher() {
  const { sessionToken } = useAdminAuth()
  const { tenantId, setTenantId } = useAdminTenant()
  const [tenants, setTenants] = useState<TenantOption[]>([])
  const [loaded, setLoaded] = useState(false)
  const [creating, setCreating] = useState(false)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [newTenantId, setNewTenantId] = useState('')
  const [newTenantName, setNewTenantName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      const response = await adminFetch('/api/admin/tenants', sessionToken)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载租户列表失败'))
      }
      const data = (await response.json()) as { tenants: TenantOption[] }
      setTenants(data.tenants)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载租户列表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken])

  useEffect(() => {
    refresh().catch((err) => console.error('租户列表刷新失败', err))
  }, [refresh])

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || creating || !newTenantId.trim() || !newTenantName.trim()) return
    setError(null)
    setCreating(true)
    try {
      const response = await adminFetch('/api/admin/tenants', sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: newTenantId.trim(), name: newTenantName.trim() }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新建租户失败'))
      }
      const created = (await response.json()) as TenantOption
      setTenants((prev) => [...prev, created])
      setTenantId(created.tenant_id)
      setNewTenantId('')
      setNewTenantName('')
      setShowCreateForm(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '新建租户失败')
    } finally {
      setCreating(false)
    }
  }

  // 列表接口挂了的兜底：至少保留一个当前 tenantId 的选项，不让下拉框
  // 整个空掉、彻底没法操作——tenantId 本身来自 TenantContext 的
  // sessionStorage 缓存，即使租户列表拉取失败也还在。
  const options = loaded && tenants.length > 0 ? tenants : [{ tenant_id: tenantId, name: tenantId, status: 'active' }]

  return (
    <div className="flex flex-col gap-2">
      <label className="flex flex-col gap-1 text-xs font-bold uppercase tracking-wide text-ink-soft">
        切换租户
        <select
          value={tenantId}
          onChange={(event) => setTenantId(event.target.value)}
          aria-label="切换租户"
          className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
        >
          {options.map((tenant) => (
            <option key={tenant.tenant_id} value={tenant.tenant_id}>
              {tenant.name}
            </option>
          ))}
        </select>
      </label>
      {error && <p role="alert" className="text-xs text-status-error">{error}</p>}
      {!showCreateForm && (
        <button
          type="button"
          onClick={() => setShowCreateForm(true)}
          className={`min-h-[36px] cursor-pointer border-2 border-ink bg-paper px-2 text-xs font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
        >
          + 新建租户
        </button>
      )}
      {showCreateForm && (
        <form onSubmit={handleCreate} className="flex flex-col gap-2 border-2 border-ink bg-paper p-2">
          <input
            value={newTenantId}
            onChange={(event) => setNewTenantId(event.target.value)}
            placeholder="tenant_id"
            aria-label="新租户 ID"
            className="border-2 border-ink bg-card px-2 py-1.5 text-xs text-ink placeholder:text-ink-soft focus:outline-none"
          />
          <input
            value={newTenantName}
            onChange={(event) => setNewTenantName(event.target.value)}
            placeholder="显示名"
            aria-label="新租户显示名"
            className="border-2 border-ink bg-card px-2 py-1.5 text-xs text-ink placeholder:text-ink-soft focus:outline-none"
          />
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || !newTenantId.trim() || !newTenantName.trim()}
              className={`min-h-[32px] flex-1 cursor-pointer border-2 border-ink bg-accent-pink px-2 text-xs font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {creating ? '创建中…' : '创建'}
            </button>
            <button
              type="button"
              onClick={() => {
                setShowCreateForm(false)
                setNewTenantId('')
                setNewTenantName('')
                setError(null)
              }}
              disabled={creating}
              className={`min-h-[32px] cursor-pointer border-2 border-ink bg-card px-2 text-xs font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
```

- [ ] **Step 2: 验证**

```bash
cd frontend && npx tsc --noEmit
```
Expected: 无报错。

手动验收（后端需先跑起来）：登录管理后台，确认下拉框展示的是真实租户列表（至少有 `demo`，因为 Task 2 的迁移会自动种进去）；点"+ 新建租户"，填一个新 `tenant_id` 提交，确认下拉框立即切到新租户且能在其它管理页面正常操作。

- [ ] **Step 3: Commit**

```bash
git add frontend/src/admin/TenantSwitcher.tsx
git commit -m "feat(frontend): fetch real tenant list and support inline tenant creation"
```

---

### Task 6: `TermsPage.tsx` 类型字段改下拉选择

**Files:**
- Modify: `frontend/src/admin/TermsPage.tsx`
- Test: `npx tsc --noEmit`（无组件测试基础设施）。

**Interfaces:**
- Consumes：既有的 `GET /api/admin/ontology/{tenant_id}/term-types`（返回 `{"term_types": [{"value": string, "extra_fields": [...]}]}`）、`GET /api/admin/ontology/product-lines`（返回 `{"product_lines": string[]}`）。

- [ ] **Step 1: 拉取枚举列表**

在 `TermsPage.tsx` 组件函数体里，紧挨着现有的 `terms`/`loaded`/`error` 几个 state 之后，加：

```tsx
  const [termTypeOptions, setTermTypeOptions] = useState<string[]>([])
  const [productLineOptions, setProductLineOptions] = useState<string[]>([])
```

加一个新的 `useEffect`（放在现有 `refresh` 相关 `useEffect` 附近），依赖 `sessionToken`/`tenantId`：

```tsx
  useEffect(() => {
    if (!sessionToken) return
    adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`, sessionToken)
      .then((res) => res.json())
      .then((data: { term_types: { value: string }[] }) =>
        setTermTypeOptions(data.term_types.map((t) => t.value)),
      )
      .catch((err) => console.error('加载实体类型枚举失败', err))
    adminFetch('/api/admin/ontology/product-lines', sessionToken)
      .then((res) => res.json())
      .then((data: { product_lines: string[] }) => setProductLineOptions(data.product_lines))
      .catch((err) => console.error('加载产品线枚举失败', err))
  }, [sessionToken, tenantId])
```

文件顶部 import 区加 `adminFetch`（`extractErrorDetail` 已经通过 `termsApi.ts` 间接使用，这里直接从 `adminApi.ts` 再导入 `adminFetch`）：

```tsx
import { adminFetch } from './adminApi'
```

- [ ] **Step 2: 新增术语表单里的下拉框**

把新增术语表单里这两个 `<input>`：

```tsx
          <input
            value={newDraft.term_type}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, term_type: event.target.value }))
            }
            placeholder="类型"
            aria-label="类型"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <input
            value={newDraft.product_line}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, product_line: event.target.value }))
            }
            placeholder="产品线"
            aria-label="产品线"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
```

换成：

```tsx
          <select
            value={newDraft.term_type}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, term_type: event.target.value }))
            }
            aria-label="类型"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
          >
            <option value="">（无类型）</option>
            {termTypeOptions.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <select
            value={newDraft.product_line}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, product_line: event.target.value }))
            }
            aria-label="产品线"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
          >
            <option value="">（无产品线）</option>
            {productLineOptions.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
```

- [ ] **Step 3: 编辑表单里的下拉框**

同样地，编辑表单（`isEditing && editDraft && (...)` 那个分支）里对应的 `term_type`/`product_line` 两个 `<input>` 也换成 `<select>`，逻辑跟 Step 2 一致（`value={editDraft.term_type}`/`onChange` 更新 `setEditDraft`），但要处理"野值"兜底：如果 `editDraft.term_type` 不在 `termTypeOptions` 里（历史脏数据或本体那边后来删了这个类型），要多渲染一个额外的 `<option>` 让当前值可见，不能让 `<select>` 静默跳到空值。写法：

```tsx
          <select
            value={editDraft.term_type}
            onChange={(event) =>
              setEditDraft((prev) => (prev ? { ...prev, term_type: event.target.value } : prev))
            }
            aria-label={`类型（${term.standard_name}）`}
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
          >
            <option value="">（无类型）</option>
            {editDraft.term_type && !termTypeOptions.includes(editDraft.term_type) && (
              <option value={editDraft.term_type}>{editDraft.term_type}（不在当前本体枚举中）</option>
            )}
            {termTypeOptions.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
```

`product_line` 的编辑 `<select>` 同样处理（用 `productLineOptions`）。

- [ ] **Step 4: 验证**

```bash
cd frontend && npx tsc --noEmit
```
Expected: 无报错。

手动验收：进术语库管理页，确认新增/编辑表单的类型和产品线都是下拉框，选项跟本体 Schema 管理页的实体类型/产品线一致；切换租户后下拉框选项跟着变。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/TermsPage.tsx
git commit -m "feat(frontend): source term_type/product_line as dropdowns from ontology schema"
```

---

### Task 7: 文档管理 / 术语库管理分页 — 后端

**Files:**
- Modify: `app/graphrag/terms_store.py`
- Modify: `app/ingestion/tracking.py`
- Modify: `app/api/admin_terms_routes.py`
- Modify: `app/api/admin_document_routes.py`
- Test: `tests/graphrag/test_terms_store.py`、`tests/graphrag/test_document_tracking.py`（如果不存在就新建，文件名以实际测试目录里 `tracking.py` 对应的既有测试文件为准）、`tests/api/test_admin_terms_routes.py`、`tests/api/test_admin_document_routes.py`

**Interfaces:**
- Produces：
  - `list_terms(conn, tenant_id, *, limit: int | None = None, offset: int = 0)`（新增两个可选关键字参数，默认值保持原有全量行为）
  - `count_terms(conn, tenant_id) -> int`
  - `list_tracked_files(conn, *, tenant_id, limit: int | None = None, offset: int = 0)`（同上）
  - `count_tracked_files(conn, *, tenant_id) -> int`

- [ ] **Step 1: `terms_store.py` 加分页参数**

`app/graphrag/terms_store.py` 里的 `list_terms` 函数：

```python
async def list_terms(conn: aiosqlite.Connection, tenant_id: str) -> list[Term]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms WHERE tenant_id = ? ORDER BY standard_name",
        (tenant_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]
```

改成（哨兵模式跟 `app/graphrag/review_queue.py::list_pending_reviews` 一致：`limit=None` 时 SQL 用 `LIMIT -1` 表示不限制，默认参数值不变，所有现有调用点行为不变）：

```python
async def list_terms(
    conn: aiosqlite.Connection, tenant_id: str, *, limit: int | None = None, offset: int = 0
) -> list[Term]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
        "extra_properties FROM terms WHERE tenant_id = ? ORDER BY standard_name LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]


async def count_terms(conn: aiosqlite.Connection, tenant_id: str) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM terms WHERE tenant_id = ?", (tenant_id,))
    row = await cursor.fetchone()
    return row[0]
```

- [ ] **Step 2: `tracking.py` 加分页参数**

`app/ingestion/tracking.py` 里的 `list_tracked_files` 函数同样改法：

```python
async def list_tracked_files(
    conn: aiosqlite.Connection, *, tenant_id: str, limit: int | None = None, offset: int = 0
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT file_path, content_hash, chunk_count, last_ingested_at "
        "FROM ingested_documents WHERE tenant_id = ? ORDER BY last_ingested_at DESC LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_tracked_files(conn: aiosqlite.Connection, *, tenant_id: str) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM ingested_documents WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    return row[0]
```

（原查询没有 `ORDER BY`，加 `ORDER BY last_ingested_at DESC` 让分页结果顺序稳定可预期——没有确定排序的分页会在翻页之间出现同一条记录在两页都出现或者漏掉的问题。）

- [ ] **Step 3: 接入 `admin_terms_routes.py` 的列表接口**

`list_all_terms` 函数：

```python
@router.get("", response_model=TermListResponse)
async def list_all_terms(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    terms = await list_terms(review_conn, tenant_id)
    return TermListResponse(terms=[_to_response(term) for term in terms])
```

改成：

```python
@router.get("", response_model=TermListResponse)
async def list_all_terms(
    tenant_id: str,
    page: int = 1,
    page_size: int = 20,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    offset = (page - 1) * page_size
    terms = await list_terms(review_conn, tenant_id, limit=page_size, offset=offset)
    total = await count_terms(review_conn, tenant_id)
    return TermListResponse(terms=[_to_response(term) for term in terms], total=total)
```

`TermListResponse` 加一个字段：

```python
class TermListResponse(BaseModel):
    terms: list[TermResponse]
    total: int
```

import 区把 `count_terms` 加进 `from app.graphrag.terms_store import (...)` 那组导入。

- [ ] **Step 4: 接入 `admin_document_routes.py` 的列表接口**

`list_documents` 函数：

```python
@router.get("", response_model=DocumentsListResponse)
async def list_documents(
    tenant_id: str,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentsListResponse:
    documents = await list_tracked_files(ingestion_conn, tenant_id=tenant_id)
    pending_jobs = await list_pending_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    dead_jobs = await list_dead_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    return DocumentsListResponse(
        documents=documents, pending_jobs=pending_jobs, dead_jobs=dead_jobs
    )
```

改成：

```python
@router.get("", response_model=DocumentsListResponse)
async def list_documents(
    tenant_id: str,
    page: int = 1,
    page_size: int = 20,
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
) -> DocumentsListResponse:
    offset = (page - 1) * page_size
    documents = await list_tracked_files(
        ingestion_conn, tenant_id=tenant_id, limit=page_size, offset=offset
    )
    total = await count_tracked_files(ingestion_conn, tenant_id=tenant_id)
    pending_jobs = await list_pending_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    dead_jobs = await list_dead_jobs(ingestion_conn, limit=50, tenant_id=tenant_id)
    return DocumentsListResponse(
        documents=documents, total=total, pending_jobs=pending_jobs, dead_jobs=dead_jobs
    )
```

`DocumentsListResponse` 定义处加 `total: int` 字段。import 区把 `count_tracked_files` 加进 `from app.ingestion.tracking import (...)` 那组导入。

- [ ] **Step 5: 补测试**

  - `tests/graphrag/test_terms_store.py`：新增用例验证 `list_terms(conn, tenant_id, limit=1, offset=1)` 能正确分页（种 3 条术语，取第 2 条），以及 `count_terms` 返回正确总数；同时确认不传 `limit`/`offset` 时行为和之前完全一致（跑一次现有的 `test_create_and_list_term...`之类的用例，不应该因为这次改动而失败）。
  - 文档追踪的对应测试文件（用 `grep -rl "list_tracked_files" tests/` 找到实际文件名）：同样加分页用例 + 确认默认行为不变。
  - `tests/api/test_admin_terms_routes.py`：验证 `GET ?page=2&page_size=1` 返回正确的那一条 + `total` 字段值正确。
  - `tests/api/test_admin_document_routes.py`：同样验证 `page`/`page_size`/`total`。

- [ ] **Step 6: 跑测试**

```bash
python -m pytest tests/graphrag/test_terms_store.py tests/api/test_admin_terms_routes.py tests/api/test_admin_document_routes.py -q -k "pag or Pag"
python -m pytest tests/graphrag/test_terms_store.py tests/api/test_admin_terms_routes.py tests/api/test_admin_document_routes.py -q
```
Expected: 全部通过（第一条命令只是先快速看一眼新加的分页用例，第二条是这三个文件的完整回归）。

- [ ] **Step 7: Commit**

```bash
git add app/graphrag/terms_store.py app/ingestion/tracking.py app/api/admin_terms_routes.py app/api/admin_document_routes.py tests/graphrag/test_terms_store.py tests/api/test_admin_terms_routes.py tests/api/test_admin_document_routes.py
git commit -m "feat(api): paginate terms and documents admin list endpoints"
```

（如果文档追踪的测试文件路径和预期不一致，把它也加进这次 commit。）

---

### Task 8: 文档管理 / 术语库管理分页 — 前端

**Files:**
- Modify: `frontend/src/admin/TermsPage.tsx`
- Modify: `frontend/src/admin/DocumentsPage.tsx`
- Modify: `frontend/src/admin/termsApi.ts`
- Test: `npx tsc --noEmit`。

**Interfaces:**
- Consumes：Task 7 产出的 `page`/`page_size` query 参数和响应里的 `total` 字段；`frontend/src/admin/Pager.tsx`（已存在，直接复用）。

- [ ] **Step 1: `termsApi.ts` 的 `fetchTerms` 加分页参数**

`frontend/src/admin/termsApi.ts` 里的 `fetchTerms`：

```ts
export async function fetchTerms(sessionToken: string, tenantId: string): Promise<TermRecord[]> {
  const response = await adminFetch(`/api/admin/${encodeURIComponent(tenantId)}/terms`, sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: TermRecord[] }
  return data.terms
}
```

改成：

```ts
export interface TermPage {
  terms: TermRecord[]
  total: number
}

export async function fetchTermsPage(
  sessionToken: string,
  tenantId: string,
  page: number,
  pageSize: number,
): Promise<TermPage> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms?page=${page}&page_size=${pageSize}`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: TermRecord[]; total: number }
  return { terms: data.terms, total: data.total }
}
```

保留原来的 `fetchTerms`（不分页、拉全量）不删——`GraphReviewsPage.tsx` 的 `fetchGraphTerms` 复用的就是这个 `fetchTerms`，用于自动补全下拉建议，那个场景需要全量数据做前端过滤，不应该被这次分页改动影响。新加一个 `fetchTermsPage` 专供 `TermsPage.tsx` 自己的列表展示使用。

- [ ] **Step 2: `TermsPage.tsx` 接入分页**

参照 `GraphReviewsPage.tsx` 里 `pendingPage`/`pendingTotal`/`Pager` 的写法（包括请求序号防旧响应覆盖新响应、增删后自动退页那两个 `useEffect`），给 `TermsPage.tsx` 加：

```tsx
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
```

`PAGE_SIZE` 常量：

```tsx
const PAGE_SIZE = 20
```

`refresh` 函数改成用 `fetchTermsPage` 而不是 `fetchTerms`：

```tsx
  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      const data = await fetchTermsPage(sessionToken, tenantId, page, PAGE_SIZE)
      setTerms(data.terms)
      setTotal(data.total)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载术语表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, page])
```

新增租户切换时回到第一页的 `useEffect`（照抄 `GraphReviewsPage.tsx` 里 `setPendingPage(1)` 那个 effect 的写法）：

```tsx
  useEffect(() => {
    setPage(1)
  }, [tenantId])
```

新增删/增后如果当页变空自动退页的 `useEffect`（照抄 `GraphReviewsPage.tsx` 对应逻辑）：

```tsx
  useEffect(() => {
    if (loaded && terms.length === 0 && page > 1) {
      setPage((p) => p - 1)
    }
  }, [loaded, terms.length, page])
```

`handleCreate`/`handleSaveEdit`/`handleDelete` 里原来 `await refresh()` 保持不变（`refresh` 已经带上当前 `page` 了）。

组件返回的 JSX 末尾（`{loaded && !error && terms.length === 0 && (...)}` 那段之后）加：

```tsx
      {loaded && terms.length > 0 && (
        <Pager page={page} totalPages={Math.max(1, Math.ceil(total / PAGE_SIZE))} onPageChange={setPage} />
      )}
```

顶部 import 加 `import { Pager } from './Pager'` 和把 `fetchTerms` 换成 `fetchTermsPage`（`createTerm`/`deleteTerm`/`updateTerm` 三个函数保留原样，只有列表读取这一处变化）。

- [ ] **Step 3: `DocumentsPage.tsx` 接入分页**

同样的模式：加 `page`/`documentsTotal` state（`PAGE_SIZE = 20`），`refresh` 函数的 URL 加 `&page=${page}&page_size=${PAGE_SIZE}`，响应体解析加 `total`：

```tsx
  const [page, setPage] = useState(1)
  const [documentsTotal, setDocumentsTotal] = useState(0)
```

（放在现有 `documents`/`pendingJobs`/`deadJobs` 几个 state 声明附近；`PAGE_SIZE` 常量参照 `GraphReviewsPage.tsx` 的写法，加在文件顶部模块级。）

```tsx
  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/documents?tenant_id=${encodeURIComponent(tenantId)}&page=${page}&page_size=${PAGE_SIZE}`,
      sessionToken,
    )
    const data = (await response.json()) as {
      documents: TrackedDocument[]
      total: number
      pending_jobs: PendingJob[]
      dead_jobs: DeadJob[]
    }
    setDocuments(data.documents)
    setDocumentsTotal(data.total)
    setPendingJobs(data.pending_jobs)
    setDeadJobs(data.dead_jobs)
    hasPendingJobsRef.current = data.pending_jobs.length > 0
    setLoaded(true)
  }, [sessionToken, tenantId, page])
```

`refresh` 的依赖数组新增 `page`——现有的自动轮询 `useEffect`（`pollNowRef.current = poll; poll()`）依赖 `refresh`，`refresh` 变了会重新挂载轮询循环，这是预期行为（换页之后轮询应该轮询新的那一页）。

加租户切换/文档增删后的退页 `useEffect`，模式跟 Task 8 Step 2 里 `TermsPage.tsx` 的一致（用 `documents.length`/`page`/`loaded` 判断）。

JSX 里"已摄取文档"区块末尾加 `<Pager .../>`，`totalPages` 用 `Math.ceil(documentsTotal / PAGE_SIZE)`。

顶部 import 加 `import { Pager } from './Pager'`。

（`pendingJobs`/`deadJobs` 两个列表本身不分页，维持现状——它们各自后端已经用 `limit=50` 兜底，本次范围内不用管。）

- [ ] **Step 4: 验证**

```bash
cd frontend && npx tsc --noEmit
```
Expected: 无报错。

手动验收：术语库管理页和文档管理页都出现分页器（前提是该租户数据量 > 20 条，验收时可以临时把 `PAGE_SIZE` 改小或者多建几条测试数据来触发分页展示，验收完记得改回 20）；翻页、删除导致当页清空后自动退页、切换租户后页码回到第一页，这几个行为都要跟 `GraphReviewsPage.tsx` 现有的分页体验一致。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/TermsPage.tsx frontend/src/admin/DocumentsPage.tsx frontend/src/admin/termsApi.ts
git commit -m "feat(frontend): paginate terms and documents admin list pages"
```

---

## 执行顺序说明

Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8。Task 1（低风险速修）和 Task 6（术语库下拉框）实际上和租户注册表那条线（Task 2/3/4/5）没有依赖关系，可以并行构思，但 subagent-driven-development 是单线程逐任务派发，按上面顺序走即可，不需要额外并行调度。
