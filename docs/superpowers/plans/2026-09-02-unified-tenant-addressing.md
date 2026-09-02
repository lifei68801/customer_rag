# 统一租户寻址 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把管理后台四组租户作用域路由的 `tenant_id` 从查询参数 / 表单字段 / JSON body 统一改为**路径参数**，使后续的租户权限校验能用单一依赖覆盖全部路由。

**Architecture:** 纯粹的接口形状改造，不改任何业务逻辑。每组路由的 `prefix` 从 `/api/admin/xxx` 改为 `/api/admin/{tenant_id}/xxx`，端点签名里的 `tenant_id` 来源随之改变，请求体模型里的 `tenant_id` 字段删除。前端四个文件同步改 URL。

**Tech Stack:** FastAPI（路由与依赖）、pytest（`asyncio_mode = "auto"`）、React + vitest。

**Spec:** `docs/superpowers/specs/2026-09-02-admin-account-system-design.md` 第 3 节

## Global Constraints

- **不改任何业务逻辑。** 本计划只改 `tenant_id` 的传递方式。任何行为变化都是缺陷。
- **`require_active_tenant_or_404` 的调用位置与调用条件一律不变。** 它是**状态**校验（租户是否启用），与将来的**权限**校验正交。现有策略是「24 个写路由有它，16 个读路由没有」，本计划不得改变任一路由的归属。
- **每写完一条否定式断言（「X 不应该出现」「不再是 Y」），必须故意破坏实现确认它变红，然后恢复。** 断言写在正确和错误实现都能通过的位置，是本项目反复出现的问题。
- 后端测试：`pytest`（仓库根目录运行）。前端：`cd frontend && npm test`、`npm run typecheck`、`npm run build`。
- Windows 环境跑 Python 输出中文需要 `PYTHONIOENCODING=utf-8`。
- 提交信息用中文，说明**为什么**而不只是做了什么。

---

## 文件结构

| 文件 | 责任 | 本计划中的改动 |
| --- | --- | --- |
| `app/api/admin_nav_badges_routes.py` | 侧边栏徽标数字，1 个 GET | prefix + 1 个端点签名 |
| `app/api/admin_duplicate_review_routes.py` | 疑似重复审核，1 GET + 2 POST | prefix + 3 个端点 + 2 个请求模型 |
| `app/api/admin_graph_review_routes.py` | 关系审核，1 GET + 2 POST | prefix + 3 个端点 + 2 个请求模型 |
| `app/api/admin_document_routes.py` | 文档管理，7 个端点 | prefix + 7 个端点（含 1 个 multipart 上传） |
| `app/api/tenant_guard.py` | 租户启用状态守卫 | **仅模块文档**——它记录的"四种来源"理由在本计划后过时 |
| `frontend/src/admin/useNavBadges.ts` | 徽标取数 | 1 处 URL |
| `frontend/src/admin/DuplicateTermSuggestionsTab.tsx` | 疑似重复页 | 3 处 URL |
| `frontend/src/admin/GraphReviewsPage.tsx` | 关系审核页 | 6 处 URL |
| `frontend/src/admin/DocumentsPage.tsx` | 文档页 | 7 处 URL |
| `frontend/src/admin/tenantAddressing.test.tsx` | **新建** | 断言前端真的请求了带租户的新路径 |

任务顺序按改动量从小到大：nav-badges（1 个端点）建立模式，documents（7 个端点 + multipart）收尾。

---

### Task 1: nav-badges 改用路径参数

**Files:**
- Modify: `app/api/admin_nav_badges_routes.py:12-14`（prefix）、`:26-29`（端点签名）
- Test: `tests/api/test_admin_nav_badges_routes.py:77-90`（`_get` 辅助函数）、`:129-136`（鉴权用例）

**Interfaces:**
- Consumes: 无
- Produces: 路径形状 `/api/admin/{tenant_id}/nav-badges`。后续三个任务照此模式改造。

- [ ] **Step 1: 改测试里的请求构造，先让它红**

`tests/api/test_admin_nav_badges_routes.py` 第 77-90 行的 `_get` 改为：

```python
def _get(review_conn, *, tenant_id: str):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        client = TestClient(app)
        return client.get(
            f"/api/admin/{tenant_id}/nav-badges",
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()
```

第 129-136 行的鉴权用例同步改：

```python
def test_requires_authentication(review_conn):
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        response = TestClient(app).get("/api/admin/demo/nav-badges")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_admin_nav_badges_routes.py -v`
Expected: 5 个用例全部 FAIL，状态码 404（新路径尚不存在）

- [ ] **Step 3: 改路由**

`app/api/admin_nav_badges_routes.py` 第 12-14 行：

```python
router = APIRouter(
    prefix="/api/admin/{tenant_id}/nav-badges",
    dependencies=[Depends(deps.require_admin_session)],
)
```

第 26-29 行的端点签名不需要改动——`tenant_id: str` 会自动被 FastAPI 识别为路径参数（因为 prefix 里声明了同名占位符）。这是本次改造的核心机制：**参数名不变，来源随路径模板变化**。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/api/test_admin_nav_badges_routes.py -v`
Expected: 5 passed

- [ ] **Step 5: 确认旧路径确实没了（防假绿）**

新增用例到 `tests/api/test_admin_nav_badges_routes.py` 末尾：

```python
def test_old_query_param_path_is_gone(review_conn):
    """旧路径必须 404。留着它等于留一条不受租户校验的旁路——而那条旁路
    不会有任何报错，只会安静地返回数据。"""
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        response = TestClient(app).get(
            "/api/admin/nav-badges",
            params={"tenant_id": "demo"},
            headers=_authed_headers(session_store),
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 6: 破坏实现，确认上一条会红**

把 prefix 临时改回 `"/api/admin/nav-badges"`，跑：

Run: `pytest tests/api/test_admin_nav_badges_routes.py::test_old_query_param_path_is_gone -v`
Expected: FAIL（旧路径返回 200，断言 404 失败）

确认变红后把 prefix 改回 `"/api/admin/{tenant_id}/nav-badges"`，重跑确认 6 passed。

- [ ] **Step 7: 提交**

```bash
git add app/api/admin_nav_badges_routes.py tests/api/test_admin_nav_badges_routes.py
git commit -m "refactor: nav-badges 的 tenant_id 改走路径参数

四组租户路由统一寻址的第一步。tenant_id 目前有四种传法（path/query/
Form/body），一个公共依赖覆盖不全，而按来源各写一个依赖就是四条各自会
被遗忘的路径。统一成路径参数之后，租户权限校验才能用单一依赖兜住。

旧路径必须 404 而不是保留兼容：留着它等于留一条不受校验的旁路，而那条
旁路不会有任何报错。"
```

---

### Task 2: duplicate-reviews 改用路径参数

**Files:**
- Modify: `app/api/admin_duplicate_review_routes.py:24-26`（prefix）、`:33-40`（两个请求模型）、`:43-49`（list）、`:59-65`（approve）、`:100-106`（reject）
- Test: `tests/api/test_admin_duplicate_review_routes.py`（5 处 URL）

**Interfaces:**
- Consumes: Task 1 建立的路径模式
- Produces: `ApproveDuplicateRequest` 不再含 `tenant_id` 字段，只剩 `keep_node_key`；`RejectDuplicateRequest` 只剩 `note`

- [ ] **Step 1: 改测试，先让它红**

把 `tests/api/test_admin_duplicate_review_routes.py` 里所有请求 URL 与 body 改成新形状。逐处对应关系：

| 旧 | 新 |
| --- | --- |
| `client.get("/api/admin/duplicate-reviews", params={"tenant_id": "demo", ...})` | `client.get("/api/admin/demo/duplicate-reviews", params={...不含 tenant_id...})` |
| `client.post(f"/api/admin/duplicate-reviews/{rid}/approve", json={"tenant_id": "demo", "keep_node_key": k})` | `client.post(f"/api/admin/demo/duplicate-reviews/{rid}/approve", json={"keep_node_key": k})` |
| `client.post(f"/api/admin/duplicate-reviews/{rid}/reject", json={"tenant_id": "demo", "note": n})` | `client.post(f"/api/admin/demo/duplicate-reviews/{rid}/reject", json={"note": n})` |

用 `grep -n "api/admin/duplicate-reviews" tests/api/test_admin_duplicate_review_routes.py` 定位全部 5 处，逐处改完。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_admin_duplicate_review_routes.py -v`
Expected: FAIL，404（新路径不存在）

- [ ] **Step 3: 改路由**

`app/api/admin_duplicate_review_routes.py` 第 24-26 行：

```python
router = APIRouter(
    prefix="/api/admin/{tenant_id}/duplicate-reviews",
    dependencies=[Depends(deps.require_admin_session)],
)
```

第 33-40 行的两个请求模型删掉 `tenant_id` 字段：

```python
class ApproveDuplicateRequest(BaseModel):
    keep_node_key: str


class RejectDuplicateRequest(BaseModel):
    note: str | None = None
```

第 59-65 行 `approve` 的签名加上 `tenant_id`，函数体里 `payload.tenant_id` 全部换成 `tenant_id`：

```python
@router.post("/{review_id}/approve")
async def approve(
    tenant_id: str,
    review_id: int,
    payload: ApproveDuplicateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await approve_duplicate_suggestion(
            review_conn,
            review_id=review_id,
            tenant_id=tenant_id,
            keep_node_key=payload.keep_node_key,
        )
```

（其后的 except 链条一字不动——那段异常映射的注释记录了真实的排查过程，与本次改造无关。）

第 100-106 行 `reject` 同样处理：

```python
@router.post("/{review_id}/reject")
async def reject(
    tenant_id: str,
    review_id: int,
    payload: RejectDuplicateRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await reject_duplicate_suggestion(
            review_conn, review_id=review_id, tenant_id=tenant_id, note=payload.note
        )
```

`list_duplicate_suggestions`（第 43-49 行）签名不变，`tenant_id: str` 自动变成路径参数。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/api/test_admin_duplicate_review_routes.py -v`
Expected: 全部 passed

- [ ] **Step 5: 确认 body 里的 tenant_id 不再被接受（防假绿）**

新增用例：

```python
def test_tenant_id_in_body_is_ignored_not_honored(review_conn):
    """body 里塞 tenant_id 必须无效。如果它还被读，那路径参数就只是装饰，
    调用方仍能用 body 指向别的租户——而这正是统一寻址要消灭的东西。

    Pydantic 默认忽略多余字段，所以这里断言的是"按路径参数生效"：往
    demo 的路径上发请求、body 里写 other，结果必须落在 demo。
    """
    review_id = asyncio.run(_seed_one(review_conn, tenant_id="demo"))
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    try:
        response = TestClient(app).post(
            f"/api/admin/demo/duplicate-reviews/{review_id}/reject",
            json={"note": "n", "tenant_id": "other"},
            headers=_authed_headers(session_store),
        )
        # 落在 demo（记录在那里），所以成功；若 body 生效则会在 other 里
        # 找不到这条记录而 404。
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()
```

`_seed_one` 若测试文件中尚不存在，按该文件已有的 seed 辅助函数形状补一个，返回 `review_id`。

- [ ] **Step 6: 破坏实现，确认上一条会红**

临时把 `reject` 里的 `tenant_id=tenant_id` 改成 `tenant_id=payload.model_extra.get("tenant_id", tenant_id)`，跑：

Run: `pytest tests/api/test_admin_duplicate_review_routes.py::test_tenant_id_in_body_is_ignored_not_honored -v`
Expected: FAIL（404，因为记录在 demo 而查的是 other）

确认后恢复。

- [ ] **Step 7: 提交**

```bash
git add app/api/admin_duplicate_review_routes.py tests/api/test_admin_duplicate_review_routes.py
git commit -m "refactor: duplicate-reviews 的 tenant_id 改走路径参数

approve/reject 此前从 JSON body 取 tenant_id，请求模型里的这个字段一并
删除。路径参数是唯一来源，body 里再塞一个也不生效——留两个来源就等于
留一条能指向别的租户的旁路。"
```

---

### Task 3: graph-reviews 改用路径参数

**Files:**
- Modify: `app/api/admin_graph_review_routes.py:33-35`（prefix）、`:43-53`（两个请求模型）、`:56-58`（list）、`:88-95`（approve）、`:164-170`（reject）
- Test: `tests/api/test_admin_graph_review_routes.py`（26 处 URL）

**Interfaces:**
- Consumes: Task 1、2 建立的模式
- Produces: `ApproveRequest` 剩 `subject_standard_name` / `object_standard_name` / `subject_term_type` / `object_term_type`；`RejectRequest` 只剩 `note`

- [ ] **Step 1: 改测试，先让它红**

`grep -n "api/admin/graph-reviews" tests/api/test_admin_graph_review_routes.py` 定位 26 处。逐处：URL 前缀插入租户段、body 删掉 `tenant_id` 键。

| 旧 | 新 |
| --- | --- |
| `"/api/admin/graph-reviews"` + `params={"tenant_id": t, "status": s, ...}` | `f"/api/admin/{t}/graph-reviews"` + `params={"status": s, ...}` |
| `f"/api/admin/graph-reviews/{rid}/approve"` + `json={"tenant_id": t, "subject_standard_name": ...}` | `f"/api/admin/{t}/graph-reviews/{rid}/approve"` + `json={"subject_standard_name": ...}` |
| `f"/api/admin/graph-reviews/{rid}/reject"` + `json={"tenant_id": t, "note": n}` | `f"/api/admin/{t}/graph-reviews/{rid}/reject"` + `json={"note": n}` |

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_admin_graph_review_routes.py -v`
Expected: FAIL，404

- [ ] **Step 3: 改路由**

第 33-35 行：

```python
router = APIRouter(
    prefix="/api/admin/{tenant_id}/graph-reviews",
    dependencies=[Depends(deps.require_admin_session)],
)
```

第 43-53 行的两个模型删掉 `tenant_id`：

```python
class ApproveRequest(BaseModel):
    subject_standard_name: str
    object_standard_name: str
    subject_term_type: str | None = None
    object_term_type: str | None = None


class RejectRequest(BaseModel):
    note: str | None = None
```

第 88-95 行 `approve`：签名首位加 `tenant_id: str`，函数体内**所有** `payload.tenant_id` 替换为 `tenant_id`。第 91 行原有的那段注释需要改写——它解释的是"为什么用 payload.tenant_id 而不是 gateway 解析的那个"，改用路径参数后理由仍然成立，但主语变了：

```python
@router.post("/{review_id}/approve")
async def approve(
    tenant_id: str,
    review_id: int,
    payload: ApproveRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict[str, bool]:
    await require_active_tenant_or_404(review_conn, tenant_id)
    # 这个路由的权威 tenant_id 是路径里的这个，不走 deps.get_terms 那套
    # 独立的 gateway_tenant_id 解析——两者在这条请求里可能不是同一个值，
    # 直接按路径参数加载术语表，避免跨租户读到错的术语表。
    terms: list[Term] = await list_terms_merged(review_conn, tenant_id)
```

第 164-170 行 `reject` 同样：签名加 `tenant_id: str`，体内 `payload.tenant_id` → `tenant_id`。

用 `grep -n "payload.tenant_id" app/api/admin_graph_review_routes.py` 确认替换无遗漏——**必须为 0 个结果**。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/api/test_admin_graph_review_routes.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_graph_review_routes.py tests/api/test_admin_graph_review_routes.py
git commit -m "refactor: graph-reviews 的 tenant_id 改走路径参数

approve/reject 此前从 JSON body 取。approve 里那段"权威 tenant_id 是哪个"
的注释一并更新——理由不变（不能用 gateway 解析的那个，会跨租户读错术语
表），但主语从 payload 变成了路径参数。"
```

---

### Task 4: documents 改用路径参数，并更新 tenant_guard 模块文档

**Files:**
- Modify: `app/api/admin_document_routes.py:55-56`（prefix）、`:180-185`（上传，`Form(...)`）、`:248-250`、`:307-309`、`:347-350`、`:394-397`、`:435-441`、`:464-470`
- Modify: `app/api/tenant_guard.py:1-30`（模块文档）
- Test: `tests/api/test_admin_document_routes.py`（32 处 URL）、`tests/api/test_tenant_guard.py`（确认未受影响）

**Interfaces:**
- Consumes: Task 1-3 的模式
- Produces: 四组路由全部统一，`tenant_guard.py` 文档反映新事实

- [ ] **Step 1: 改测试，先让它红**

`grep -n "api/admin/documents" tests/api/test_admin_document_routes.py` 定位 32 处。三类改法：

| 类型 | 旧 | 新 |
| --- | --- | --- |
| GET/DELETE（query） | `"/api/admin/documents"` + `params={"tenant_id": t, ...}` | `f"/api/admin/{t}/documents"` + `params={...不含 tenant_id...}` |
| POST 上传（multipart） | `data={"tenant_id": t, "build_graph": "false"}` | `f"/api/admin/{t}/documents"`，`data={"build_graph": "false"}` |
| jobs 子路径 | `f"/api/admin/documents/jobs/{jid}/retry"` + `params={"tenant_id": t}` | `f"/api/admin/{t}/documents/jobs/{jid}/retry"` |

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_admin_document_routes.py -v`
Expected: FAIL，404

- [ ] **Step 3: 改路由**

第 55-56 行：

```python
router = APIRouter(
    prefix="/api/admin/{tenant_id}/documents",
    dependencies=[Depends(deps.require_admin_session)],
)
```

第 185 行的上传端点，`tenant_id: str = Form(...)` 改为普通路径参数。注意它在参数列表里的位置——`Form(...)` 参数必须在带默认值的参数之间，改成路径参数后没有这个约束，但为可读性放在首位：

```python
@router.post("", response_model=UploadResponse)
async def upload_document(
    tenant_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile,
    build_graph: bool = Form(False),
    upload_dir: Path = Depends(deps.get_upload_dir),
    ingestion_conn: aiosqlite.Connection = Depends(deps.get_ingestion_conn),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
```

（其余参数保持原样，不要重排。）

第 250、309、350、397、437、466 行的 `tenant_id: str` 全部不需要改动——它们已经是无默认值的普通参数，加上 prefix 里的占位符后自动变成路径参数。

**`_validate_tenant_id`（第 76-89 行）的调用一律保留。** 它校验的是 `tenant_id` 能否安全当目录名用，与来源无关；改成路径参数后这个校验反而更重要——路径参数会直接进 `Path` 拼接。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/api/test_admin_document_routes.py -v`
Expected: 全部 passed

- [ ] **Step 5: 加一条路由遮蔽测试**

新建 `tests/api/test_admin_route_shapes.py`：

```python
"""管理后台路由形状的结构测试。

四组租户路由改成 /api/admin/{tenant_id}/xxx 之后，它们和非租户路由
（/api/admin/auth/*、/api/admin/tenants*）在同一个命名空间下。路由遮蔽
是静默的——被遮蔽的那条不会报错，只会永远匹配不到，或者匹配到错误的
处理函数。
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import app


def _admin_paths() -> list[str]:
    return [
        r.path for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/api/admin")
    ]


def test_four_route_groups_are_tenant_scoped():
    """这四组必须带上租户段。少一组就是少一块将来校验不到的地方。"""
    paths = _admin_paths()
    for suffix in ("nav-badges", "duplicate-reviews", "graph-reviews", "documents"):
        matching = [p for p in paths if suffix in p]
        assert matching, f"没有找到 {suffix} 的任何路由"
        for path in matching:
            assert "{tenant_id}" in path, f"{path} 缺少租户段"


def test_non_tenant_routes_stay_non_tenant():
    """登录和租户管理不属于任何租户。给它们加上租户段会让 FastAPI 把
    tenant_id 当成必填参数，登录接口直接 422——而那时谁也进不来。"""
    for path in _admin_paths():
        if path.startswith("/api/admin/auth") or path.startswith("/api/admin/tenants"):
            assert "{tenant_id}" not in path, f"{path} 不该有租户段"


def test_no_route_is_shadowed_by_the_tenant_wildcard():
    """/api/admin/{tenant_id}/xxx 是通配路径，它不能吃掉同段数的静态路径。

    例：/api/admin/auth/login 与 /api/admin/{tenant_id}/documents 段数相同。
    两者末段不同所以安全，但将来若新增 /api/admin/auth/documents 就会被
    遮蔽。这条测试把这个约束固定下来。
    """
    static_second_segments = {
        p.split("/")[3] for p in _admin_paths()
        if len(p.split("/")) > 3 and not p.split("/")[3].startswith("{")
    }
    wildcard_third_segments = {
        p.split("/")[4] for p in _admin_paths()
        if len(p.split("/")) > 4 and p.split("/")[3] == "{tenant_id}"
    }
    for static in static_second_segments:
        conflicting = [
            p for p in _admin_paths()
            if p.startswith(f"/api/admin/{static}/")
            and len(p.split("/")) > 4
            and p.split("/")[4] in wildcard_third_segments
        ]
        assert not conflicting, f"这些路由会被租户通配路径遮蔽：{conflicting}"
```

- [ ] **Step 6: 破坏实现，确认第一条会红**

把 `admin_nav_badges_routes.py` 的 prefix 临时改回 `"/api/admin/nav-badges"`，跑：

Run: `pytest tests/api/test_admin_route_shapes.py::test_four_route_groups_are_tenant_scoped -v`
Expected: FAIL，提示 `/api/admin/nav-badges 缺少租户段`

确认后恢复。

- [ ] **Step 7: 更新 tenant_guard.py 的模块文档**

`app/api/tenant_guard.py` 第 1-30 行的模块 docstring 里，「更硬的一条」那一段现在**已经不成立**——四种来源已经统一。整段替换为：

```python
"""写路由的租户守卫：校验租户已启用，未启用就直接以 404 拒绝。

为什么是一个显式调用而不是 FastAPI 依赖：

这条守卫只加在**写**路由上。全部管理后台路由里，写操作（POST/PUT/DELETE）
都有它，读操作（GET）都没有，两个方向零例外——这是一条被一致执行的策略
（读一个停用租户的数据放行，写它不放行），不是散落的疏漏。FastAPI 的
`dependencies=[...]` 只能挂在 router 或单个 endpoint 上，没有"只作用于写
方法"这一档，挂到 router 级会连带给读路由加上守卫，改变它们的行为。

（历史注记：此前还有第二条更硬的理由——tenant_id 有路径参数、`Form(...)`、
请求体模型三种来源，一个声明了 `tenant_id: str` 的依赖对后两种会直接 422。
2026-09-02 的统一租户寻址改造已经把全部租户路由的 tenant_id 收敛为路径
参数，那条理由不再成立。留下"只作用于写方法"这一条。）

这个模块收敛的是调用之后那段把 TenantNotFoundError 翻译成 404 的四行样板
——它此前在 6 个路由文件里被逐字抄了 24 遍。
"""
```

- [ ] **Step 8: 跑后端全量**

Run: `pytest`
Expected: 全部 passed（含 `tests/api/test_tenant_guard.py` 不受影响）

- [ ] **Step 9: 提交**

```bash
git add app/api/admin_document_routes.py app/api/tenant_guard.py tests/api/test_admin_document_routes.py tests/api/test_admin_route_shapes.py
git commit -m "refactor: documents 的 tenant_id 改走路径参数，四组路由统一完成

上传接口的 tenant_id 此前是 Form(...) 字段——正是它让"用一个依赖统一校验
租户"变得不可能（Form 需要 multipart 解析，混进公共依赖会破坏非 multipart
请求）。

新增 test_admin_route_shapes.py：租户通配路径 /api/admin/{tenant_id}/xxx
和静态路径 /api/admin/auth/* 在同一命名空间下，路由遮蔽是静默的——被遮蔽
的那条不报错，只是永远匹配不到。

tenant_guard.py 的模块文档里"四种来源所以不能用依赖"那段已经不成立，改成
历史注记——留着会让下一个人照着它继续写显式调用。"
```

---

### Task 5: 前端同步改 URL

**Files:**
- Modify: `frontend/src/admin/useNavBadges.ts:25-28`
- Modify: `frontend/src/admin/DuplicateTermSuggestionsTab.tsx:37`、`:67`、`:90`
- Modify: `frontend/src/admin/GraphReviewsPage.tsx:177`、`:363`、`:408`、`:442`、`:476`、`:523`
- Modify: `frontend/src/admin/DocumentsPage.tsx:131`、`:199`、`:230`、`:253`、`:283`、`:316`、`:344`
- Create: `frontend/src/admin/tenantAddressing.test.tsx`

**Interfaces:**
- Consumes: Task 1-4 的新路径
- Produces: 前端与后端路径一致

- [ ] **Step 1: 先写断言真实 URL 的测试，让它红**

现有前端测试的 fetch stub 用 `url.includes('/nav-badges')` 这类模糊匹配——**新旧路径都能匹配**，所以改完 URL 它们照样绿，证明不了任何事。新建 `frontend/src/admin/tenantAddressing.test.tsx`：

```tsx
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'

/**
 * 前端请求的路径里必须真的带上租户段。
 *
 * 既有测试的 fetch stub 都用 url.includes('/nav-badges') 这类模糊匹配，
 * 新旧路径都能命中——改错了也是绿的。这个文件断言的是**完整路径**。
 */

const TENANT = 'demo'
let calls: string[] = []

function stubApi() {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      }
      if (url.includes('/documents')) {
        return json({ documents: [], total: 0, pending_jobs: [], dead_jobs: [] })
      }
      if (url.includes('/graph-reviews')) return json({ reviews: [], total: 0 })
      if (url.includes('/duplicate-reviews')) return json({ suggestions: [], total: 0 })
      if (url.includes('/api/admin/tenants')) {
        return json({ tenants: [{ tenant_id: TENANT, name: '演示租户', status: 'active' }] })
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  sessionStorage.setItem('admin_current_tenant', TENANT)
  localStorage.clear()
  stubApi()
})

function renderAt(path: string) {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

/** 请求过的、命中这个片段的完整 URL。 */
const callsMatching = (fragment: string) => calls.filter((u) => u.includes(fragment))

describe('前端请求带上租户段', () => {
  it('侧边栏徽标走 /api/admin/{tenant}/nav-badges', async () => {
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(callsMatching('/nav-badges').length).toBeGreaterThan(0))
    for (const url of callsMatching('/nav-badges')) {
      expect(url).toContain(`/api/admin/${TENANT}/nav-badges`)
      // 旧形状必须绝迹：查询参数里再带一个 tenant_id 说明改漏了。
      expect(url).not.toContain('nav-badges?tenant_id=')
    }
  })

  it('文档列表走 /api/admin/{tenant}/documents', async () => {
    renderAt(ADMIN_ROUTES.documents)
    await waitFor(() => expect(callsMatching('/documents').length).toBeGreaterThan(0))
    for (const url of callsMatching('/documents')) {
      expect(url).toContain(`/api/admin/${TENANT}/documents`)
    }
  })

  it('关系审核走 /api/admin/{tenant}/graph-reviews', async () => {
    renderAt(ADMIN_ROUTES.reviewRelations)
    await waitFor(() => expect(callsMatching('/graph-reviews').length).toBeGreaterThan(0))
    for (const url of callsMatching('/graph-reviews')) {
      expect(url).toContain(`/api/admin/${TENANT}/graph-reviews`)
    }
  })

  it('疑似重复走 /api/admin/{tenant}/duplicate-reviews', async () => {
    renderAt(ADMIN_ROUTES.reviewDuplicates)
    await waitFor(() => expect(callsMatching('/duplicate-reviews').length).toBeGreaterThan(0))
    for (const url of callsMatching('/duplicate-reviews')) {
      expect(url).toContain(`/api/admin/${TENANT}/duplicate-reviews`)
    }
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/tenantAddressing.test.tsx`
Expected: 4 个用例全部 FAIL（当前请求的还是旧路径）

- [ ] **Step 3: 改 useNavBadges.ts**

第 25-28 行：

```ts
        const res = await adminFetch(
          `/api/admin/${encodeURIComponent(tenantId)}/nav-badges`,
          sessionToken,
        )
```

- [ ] **Step 4: 改 DuplicateTermSuggestionsTab.tsx**

第 37 行：

```ts
        `/api/admin/${encodeURIComponent(tenantId)}/duplicate-reviews?page=${page}&page_size=${PAGE_SIZE}`,
```

第 67 行（approve）与第 90 行（reject）：URL 加租户段，同时**从 body 里删掉 `tenant_id` 键**：

```ts
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/duplicate-reviews/${reviewId}/approve`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ keep_node_key: keepNodeKey }),
        },
      )
```

reject 的 body 同理，只留 `{ note }`。

- [ ] **Step 5: 改 GraphReviewsPage.tsx**

第 177 行、363 行（两个列表）：URL 前缀加租户段，query 里删掉 `tenant_id`：

```ts
        `/api/admin/${encodeURIComponent(tenantId)}/graph-reviews?status=pending&page=${pendingPage}&page_size=${PAGE_SIZE}`,
```

第 408、442 行（单条 approve/reject）和第 476、523 行（批量）：URL 加租户段，body 删掉 `tenant_id` 键。批量那两处在循环里，注意每次迭代都要用同一个 `tenantId`。

- [ ] **Step 6: 改 DocumentsPage.tsx**

第 131、230、253、283、316、344 行：URL 前缀加租户段，query 里删掉 `tenant_id`。

第 199 行是 multipart 上传，URL 加租户段，同时**从 FormData 里删掉 `tenant_id`**：

```ts
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/documents`,
        sessionToken,
        { method: 'POST', body: formData },
      )
```

对应地，构造 `formData` 的地方删掉 `formData.append('tenant_id', tenantId)` 这一行。用 `grep -n "tenant_id" frontend/src/admin/DocumentsPage.tsx` 确认无残留。

- [ ] **Step 7: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/tenantAddressing.test.tsx`
Expected: 4 passed

- [ ] **Step 8: 破坏实现，确认否定断言会红**

把 `useNavBadges.ts` 临时改回 `/api/admin/nav-badges?tenant_id=...`，跑：

Run: `cd frontend && npx vitest run src/admin/tenantAddressing.test.tsx -t nav-badges`
Expected: FAIL

确认后恢复。

- [ ] **Step 9: 前端全量验证**

```bash
cd frontend
npm test
npm run typecheck
npm run build
```

Expected: 全绿。既有测试的模糊 stub 仍然匹配，所以它们不该有变化——**如果有测试变红，说明改动超出了 URL 范围**，回头检查。

- [ ] **Step 10: 提交**

```bash
git add frontend/src/admin/useNavBadges.ts frontend/src/admin/DuplicateTermSuggestionsTab.tsx frontend/src/admin/GraphReviewsPage.tsx frontend/src/admin/DocumentsPage.tsx frontend/src/admin/tenantAddressing.test.tsx
git commit -m "refactor(frontend): 四组接口的 URL 跟着后端带上租户段

新增 tenantAddressing.test.tsx 断言完整路径。既有测试的 fetch stub 全都用
url.includes('/nav-badges') 这类模糊匹配，新旧路径都能命中——改错了也是
绿的，证明不了任何事。这个文件断言的是完整路径和旧形状的绝迹。"
```

---

## 阶段验收

全部 5 个任务完成后：

```bash
pytest                      # 后端全量
cd frontend && npm test && npm run typecheck && npm run build
```

**手工验证**（自动化测试覆盖不到真实的 multipart 上传与浏览器行为）：

1. 启动前后端：`powershell -File scripts/start-backend.ps1` / `start-frontend.ps1`
2. 文档页上传一个 `.md` 文件，确认摄取任务出现在队列里
3. 关系审核页批准一条待审关系
4. 疑似重复页处理一条建议
5. 侧边栏徽标数字正常显示
6. 切换租户，确认以上四处的数据跟着变

**若出现 404**：说明前端某处 URL 改漏了，`grep -rn "api/admin/documents\|api/admin/graph-reviews\|api/admin/duplicate-reviews\|api/admin/nav-badges" frontend/src` 应当**零结果**（新路径里租户段在中间，不会命中这些字面量）。
