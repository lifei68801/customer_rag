# 知识图谱审核标准名校验与批量操作 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复知识图谱人工审核里 subject/object 标准名字段"必填但完全没校验"的问题——补上术语表查询接口、前端可搜索自动补全、后端强校验三层，堵住绕过封闭词表往图谱里写任意实体的口子；同时给待审核列表加上批量驳回和批量通过（逐条改对后一次性提交），减少审核员重复点击。

**Architecture:** 术语表（`app/graphrag/ontology.py::Term`）目前只在后端内部用（摄取时的自动对齐、抽取阶段的模糊匹配建议），从未经过 API 层暴露给前端，管理后台也没有任何页面能查它——这是"审核员填标准名时无从下手"的根源。新增一个只读接口把术语表整体透出给前端，前端用它驱动一个可搜索的自动补全输入框替换掉现在的自由文本框。但前端约束终究只是体验层面的，`approve_review()` 本身（被 API 路由和 CLI 两个入口共用）才是真正应该拦截"标准名不在术语表里"的地方——加一层校验，两个入口自动一起获得保护，不需要在各自入口重复实现。批量操作不新增后端接口：批量驳回和批量通过都是前端循环调用现有的单条 `/approve`、`/reject` 接口，靠现有的每条独立校验/独立错误处理天然获得"部分成功部分失败"的正确语义。

**Tech Stack:** 后端 FastAPI + aiosqlite，测试用 pytest。前端 React + TypeScript——项目没有配置任何前端自动化测试框架，验证手段是 `npm run typecheck` + `npm run build` + 手动核对。

## Global Constraints

- `approve_review()` 新增的 `terms: list[Term]` 参数没有默认值（沿用这个函数里 `now: datetime` 已经确立的先例：安全相关的必需信息不给默认值，遗漏了要在调用处报错，而不是在函数内部悄悄放行）。
- 校验只比较 `standard_name`，不接受 `aliases`——前端自动补全搜索时可以匹配别名，但选中后填进输入框、提交给后端的必须是别名对应的那个 `standard_name`，跟摄取阶段 `normalization.py::resolve_to_standard_name()` 的别名→标准名归一化行为保持一致，避免图谱里同一个实体因为拼法不同（标准名 vs 别名）被建成两个节点。
- 新增的 `GET /api/admin/graph-reviews/terms` 接口只返回 `standard_name` 和 `aliases`，不返回 `term_type`/`product_line`——这两个字段现在没有任何前端代码要用，先不加，YAGNI。
- 批量操作不新增后端接口，前端循环调用现有的单条 `/approve`、`/reject`；批量选择状态只在当前页有效，翻页/切租户/重新拉取待审核列表都会清空选中。
- 批量通过要求被选中的每一行都已经在输入框里填好了非空的 subject/object 值（跟现在单条"批准"按钮的禁用逻辑一样），不允许选中还没填的行直接提交——批量只是省去逐条点击"批准"，不能跳过逐条人工判断这一步。

---

## Task 1: 新增术语表查询接口

**Files:**
- Modify: `app/api/admin_graph_review_routes.py`
- Test: `tests/api/test_admin_graph_review_routes.py`

**Interfaces:**
- Produces: `GET /api/admin/graph-reviews/terms` → `{"terms": [{"standard_name": str, "aliases": list[str]}, ...]}`，复用路由器已有的 `require_admin_session` 依赖（跟其它 `/api/admin/graph-reviews/*` 路由一样要求登录态），内部读 `Depends(deps.get_terms)`（已存在的依赖，进程级单例缓存，`app/api/deps.py:212-217`）。

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_graph_review_routes.py` 顶部的 import 区加一行：

```python
from app.graphrag.ontology import Term
```

在文件末尾追加：

```python
def test_list_terms_returns_all_terms_from_settings():
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_terms] = lambda: [
        Term(
            standard_name="示例错误码E502", aliases=["网关超时"],
            term_type="error_code", product_line="核心平台",
        ),
        Term(
            standard_name="示例登录模块", aliases=["认证模块"],
            term_type="module", product_line="核心平台",
        ),
    ]
    try:
        client = TestClient(app)
        response = client.get(
            "/api/admin/graph-reviews/terms",
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["terms"] == [
        {"standard_name": "示例错误码E502", "aliases": ["网关超时"]},
        {"standard_name": "示例登录模块", "aliases": ["认证模块"]},
    ]


def test_list_terms_without_session_token_returns_401():
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: AdminSessionStore()
    try:
        client = TestClient(app)
        response = client.get("/api/admin/graph-reviews/terms")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_graph_review_routes.py -k test_list_terms -v`
Expected: FAIL——`GET /api/admin/graph-reviews/terms` 这个路由还不存在，返回 404（`test_list_terms_without_session_token_returns_401` 也会失败，因为期待的 401 实际是 404）。

- [ ] **Step 3: 实现**

在 `app/api/admin_graph_review_routes.py` 顶部 import 区，`from app.graphrag.neo4j_client import Neo4jGraphClient` 那一行之后加：

```python
from app.graphrag.ontology import Term
```

在 `ReviewListResponse` 类定义（当前第 28-30 行）之后插入：

```python
class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]


class TermListResponse(BaseModel):
    terms: list[TermResponse]
```

在 `list_reviews` 路由函数（当前第 44-73 行）结束之后、`approve` 路由函数（当前第 76 行）之前插入：

```python
@router.get("/terms", response_model=TermListResponse)
async def list_terms(terms: list[Term] = Depends(deps.get_terms)) -> TermListResponse:
    return TermListResponse(
        terms=[
            TermResponse(standard_name=term.standard_name, aliases=term.aliases)
            for term in terms
        ]
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_graph_review_routes.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_graph_review_routes.py tests/api/test_admin_graph_review_routes.py
git commit -m "feat(api): expose the closed-vocabulary term list to the admin frontend"
```

---

## Task 2: approve_review() 校验标准名必须在术语表中

**Files:**
- Modify: `app/graphrag/review_queue.py`
- Modify: `app/api/admin_graph_review_routes.py`
- Modify: `app/graphrag/review_cli.py`
- Test: `tests/graphrag/test_review_queue.py`
- Test: `tests/api/test_admin_graph_review_routes.py`
- Test: `tests/graphrag/test_review_cli.py`

**Interfaces:**
- Produces:
  - `review_queue.py` 新增 `StandardNameNotInTermsError(Exception)`。
  - `approve_review(..., terms: list[Term])`（新增必填参数，插在 `graph_client` 和 `now` 之间）——两侧标准名有一个不在 `{t.standard_name for t in terms}` 里就抛 `StandardNameNotInTermsError`，此时既不写图谱也不把 review 标记为已处理（保持 pending，方便审核员改对后重试）。
  - API 路由 `POST /{review_id}/approve` 捕获 `StandardNameNotInTermsError` 返回 400，`detail` 是异常的字符串消息。
  - `review_cli.py::cmd_approve(..., terms: list[Term])`（新增必填参数）。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_review_queue.py` 顶部 import 区，把：

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

改成：

```python
from app.graphrag.ontology import Term
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    StandardNameNotInTermsError,
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

在 `_NOW = datetime(2026, 8, 12, 12, 0, 0)` 那一行之后加一个测试用的术语构造辅助函数：

```python
def _terms(*standard_names: str) -> list[Term]:
    return [
        Term(standard_name=name, aliases=[], term_type="", product_line="")
        for name in standard_names
    ]
```

在文件末尾追加新测试：

```python
async def test_approve_review_rejects_standard_name_not_in_terms():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    with pytest.raises(StandardNameNotInTermsError):
        await approve_review(
            conn,
            review_id=review_id,
            subject_standard_name="不在术语表里的名字",
            object_standard_name="示例登录模块",
            tenant_id="t1",
            graph_client=graph_client,
            terms=_terms("示例登录模块"),
            now=_NOW,
        )

    # 校验失败不写图谱、也不改变 review 状态——还留在待审核队列里，
    # 方便审核员改对了标准名之后重新提交
    assert graph_client.written == []
    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["review_id"] == review_id
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py -k test_approve_review_rejects_standard_name_not_in_terms -v`
Expected: FAIL——`ImportError: cannot import name 'StandardNameNotInTermsError'`（还没实现）。

- [ ] **Step 3: 实现**

在 `app/graphrag/review_queue.py` 顶部 import 区，`from app.graphrag.provenance import HUMAN_APPROVED` 那一行之后加：

```python
from app.graphrag.ontology import Term
```

在 `ReviewAlreadyResolvedError` 类定义（当前第 50-52 行）之后插入：

```python
class StandardNameNotInTermsError(Exception):
    """人工确认的标准名（subject 或 object）不在当前术语表里——阻止绕开
    封闭词表的强约束、把术语表里没有的任意字符串当成新术语写进图谱。
    前端自动补全只是体验层面的约束，这里才是真正的安全边界：API 路由和
    review_cli.py 两个批准入口都调用同一个 approve_review()，校验只需要
    加在这一处。"""
```

把 `approve_review` 函数（当前第 252-289 行）整个替换成：

```python
async def approve_review(
    conn: aiosqlite.Connection,
    *,
    review_id: int,
    subject_standard_name: str,
    object_standard_name: str,
    tenant_id: str,
    graph_client: ReviewGraphClientProtocol,
    terms: list[Term],
    now: datetime,
) -> None:
    """人工确认候选关系对应的标准名称后，写入图谱并把队列状态标记为已批准。

    写入的边标记 provenance=HUMAN_APPROVED（见 app/graphrag/provenance.py）
    ——这是这条边第一次、也是唯一一次被写入图谱的时刻（进了审核队列的
    候选，在此之前从未调用过 merge_relation），与自动写入路径共用同一个
    Neo4jGraphClient.merge_relation，只是 provenance 标记不同。

    terms 是当前生效的封闭词表，两侧标准名必须在其中，见
    StandardNameNotInTermsError 的说明。校验失败时不写图谱、也不改变
    review 状态（仍是 pending），方便审核员改对后重新提交，而不是必须
    先驳回再重新走一遍抽取流程。
    """
    row = await _fetch_pending_row(conn, review_id, tenant_id=tenant_id)
    valid_standard_names = {term.standard_name for term in terms}
    if subject_standard_name not in valid_standard_names:
        raise StandardNameNotInTermsError(
            f"subject_standard_name 不在术语表中: {subject_standard_name!r}"
        )
    if object_standard_name not in valid_standard_names:
        raise StandardNameNotInTermsError(
            f"object_standard_name 不在术语表中: {object_standard_name!r}"
        )
    await graph_client.merge_relation(
        subject_standard_name=subject_standard_name,
        object_standard_name=object_standard_name,
        relation_type=row["relation_type"],
        source=row["source"],
        tenant_id=tenant_id,
        provenance=HUMAN_APPROVED,
        recorded_at=now,
    )
    # WHERE 里重复加 tenant_id：单看这条语句本身，_fetch_pending_row 已经
    # 校验过 review_id 属于这个 tenant_id，此刻再查一次理论上多余；但两条
    # 语句一旦将来被拆开（比如中间插入别的逻辑），少了这层防御就会变成
    # "校验和实际更新对不上号" 的隐患，多写一个条件的成本很低。
    await conn.execute(
        "UPDATE graph_review_queue SET status='approved', "
        "resolved_at=datetime('now'), resolved_note=? "
        "WHERE review_id=? AND tenant_id=?",
        (f"{subject_standard_name} -> {object_standard_name}", review_id, tenant_id),
    )
    await conn.commit()
```

现在更新本文件里其它已有的 `approve_review()` 调用点，加上 `terms=` 参数——`test_approve_review_writes_relation_with_source_and_tenant_and_removes_from_pending`（约第 100-108 行）：

```python
    await approve_review(
        conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
        terms=_terms("示例错误码E502", "示例登录模块"),
        now=_NOW,
    )
```

`test_approve_review_from_wrong_tenant_raises_not_found`（约第 132-137 行，租户不对会在读取 terms 之前就因为 `_fetch_pending_row` 抛 `ReviewNotFoundError`，传空列表即可）：

```python
    with pytest.raises(ReviewNotFoundError):
        await approve_review(
            conn, review_id=review_id, subject_standard_name="x",
            object_standard_name="y", tenant_id="t2", graph_client=graph_client,
            terms=[], now=_NOW,
        )
```

`test_approve_unknown_review_id_raises`（约第 178-187 行，同样在校验术语表之前就已经因为 review_id 不存在抛错，传空列表即可）：

```python
    with pytest.raises(ReviewNotFoundError):
        await approve_review(
            conn,
            review_id=999,
            subject_standard_name="a",
            object_standard_name="b",
            tenant_id="t1",
            graph_client=graph_client,
            terms=[],
            now=_NOW,
        )
```

`test_approve_already_resolved_review_raises`（约第 199-208 行，同样在术语表校验之前就已经因为已处理过抛错，传空列表即可）：

```python
    with pytest.raises(ReviewAlreadyResolvedError):
        await approve_review(
            conn,
            review_id=review_id,
            subject_standard_name="a",
            object_standard_name="b",
            tenant_id="t1",
            graph_client=graph_client,
            terms=[],
            now=_NOW,
        )
```

`test_list_resolved_reviews_returns_approved_and_rejected_ordered_by_resolved_at`（约第 262-265 行）：

```python
    await approve_review(
        conn, review_id=approved_id, subject_standard_name="A", object_standard_name="B",
        tenant_id="t1", graph_client=graph_client, terms=_terms("A", "B"), now=_NOW,
    )
```

`test_count_resolved_reviews_matches_status_filter`（约第 343-346 行）：

```python
    await approve_review(
        conn, review_id=approved_id, subject_standard_name="A", object_standard_name="B",
        tenant_id="t1", graph_client=graph_client, terms=_terms("A", "B"), now=_NOW,
    )
```

现在改 `app/api/admin_graph_review_routes.py`。顶部 import 区加：

```python
from app.graphrag.ontology import Term
```

把 review_queue 的 import（当前第 12-21 行）：

```python
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
```

改成：

```python
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    StandardNameNotInTermsError,
    approve_review,
    count_pending_reviews,
    count_resolved_reviews,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)
```

把 `approve` 路由函数（当前第 76-97 行）整个替换成：

```python
@router.post("/{review_id}/approve")
async def approve(
    review_id: int,
    payload: ApproveRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    terms: list[Term] = Depends(deps.get_terms),
) -> dict[str, bool]:
    try:
        await approve_review(
            review_conn,
            review_id=review_id,
            subject_standard_name=payload.subject_standard_name,
            object_standard_name=payload.object_standard_name,
            tenant_id=payload.tenant_id,
            graph_client=graph_client,
            terms=terms,
            now=datetime.now(),
        )
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="待审核记录不存在")
    except ReviewAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="该记录已经处理过")
    except StandardNameNotInTermsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"approved": True}
```

现在更新 `tests/api/test_admin_graph_review_routes.py` 里所有会真正走到 `merge_relation`（即校验会生效）的 approve 调用点，加上 `deps.get_terms` 的 override。`test_approve_review_calls_graph_client_and_moves_to_history`（第一段 dependency_overrides 设置处，约第 95-98 行）：

```python
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: graph_client
    app.dependency_overrides[deps.get_terms] = lambda: [
        Term(standard_name="A", aliases=[], term_type="", product_line=""),
        Term(standard_name="B", aliases=[], term_type="", product_line=""),
    ]
```

`test_approve_already_resolved_review_returns_409`（约第 199-202 行）：

```python
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    app.dependency_overrides[deps.get_terms] = lambda: [
        Term(standard_name="A", aliases=[], term_type="", product_line=""),
        Term(standard_name="B", aliases=[], term_type="", product_line=""),
    ]
```

`test_list_reviews_status_all_returns_both_approved_and_rejected`（约第 266-269 行）：

```python
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    app.dependency_overrides[deps.get_terms] = lambda: [
        Term(standard_name="A", aliases=[], term_type="", product_line=""),
        Term(standard_name="B", aliases=[], term_type="", product_line=""),
    ]
```

（`test_approve_nonexistent_review_returns_404` 不用改——404 在 `_fetch_pending_row` 就已经抛出，永远走不到术语表校验，不覆盖 `deps.get_terms` 时它会退回真实的 `load_terms_from_settings()`，加载 `app/graphrag/terminology_seed.yaml`，这是个能正常解析的真实文件，不会报错，只是校验逻辑根本没机会跑到。）

新增一条测试，专门验证这一层校验通过 API 是可达的（不是只在 `review_queue.py` 单元测试里验证）。在文件末尾追加：

```python
def test_approve_review_with_standard_name_not_in_terms_returns_400(review_conn):
    review_id = asyncio.run(
        enqueue_for_review(
            review_conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
    )
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: FakeGraphClient()
    app.dependency_overrides[deps.get_terms] = lambda: [
        Term(standard_name="B", aliases=[], term_type="", product_line=""),
    ]
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/admin/graph-reviews/{review_id}/approve",
            json={"tenant_id": "t1", "subject_standard_name": "A", "object_standard_name": "B"},
            headers=_authed_headers(session_store),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
```

（这个测试用 `review_conn` fixture 参数，跟本文件其它测试的写法一致。）

现在改 `app/graphrag/review_cli.py`。顶部 import 区加：

```python
from app.graphrag.ontology import Term
```

把 `from app.graphrag.factory import build_graph_client_from_settings` 那一行改成：

```python
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
```

把 `cmd_approve` 函数（当前第 43-64 行）整个替换成：

```python
async def cmd_approve(
    *,
    review_conn: aiosqlite.Connection,
    review_id: int,
    subject_standard_name: str,
    object_standard_name: str,
    tenant_id: str,
    graph_client: ReviewGraphClientProtocol,
    terms: list[Term],
) -> None:
    await approve_review(
        review_conn,
        review_id=review_id,
        subject_standard_name=subject_standard_name,
        object_standard_name=object_standard_name,
        tenant_id=tenant_id,
        graph_client=graph_client,
        terms=terms,
        now=datetime.now(),
    )
    print(
        f"已批准 review_id={review_id}，写入图谱："
        f"{subject_standard_name} -> {object_standard_name}"
    )
```

把 `_main()` 里 `elif args.command == "approve":` 分支（当前第 115-124 行）替换成：

```python
    elif args.command == "approve":
        graph_client = build_graph_client_from_settings(settings)
        terms = load_terms_from_settings(settings)
        await cmd_approve(
            review_conn=review_conn,
            review_id=args.review_id,
            subject_standard_name=args.subject,
            object_standard_name=args.object,
            tenant_id=args.tenant_id,
            graph_client=graph_client,
            terms=terms,
        )
```

现在更新 `tests/graphrag/test_review_cli.py`。顶部 import 区加：

```python
from app.graphrag.ontology import Term
```

把 `test_cmd_approve_writes_relation_via_graph_client` 里的 `cmd_approve(...)` 调用（约第 82-89 行）：

```python
    await cmd_approve(
        review_conn=conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
    )
```

改成：

```python
    await cmd_approve(
        review_conn=conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
        terms=[
            Term(
                standard_name="示例错误码E502", aliases=[],
                term_type="", product_line="",
            ),
            Term(
                standard_name="示例登录模块", aliases=[],
                term_type="", product_line="",
            ),
        ],
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_review_queue.py tests/api/test_admin_graph_review_routes.py tests/graphrag/test_review_cli.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/review_queue.py app/api/admin_graph_review_routes.py app/graphrag/review_cli.py \
  tests/graphrag/test_review_queue.py tests/api/test_admin_graph_review_routes.py tests/graphrag/test_review_cli.py
git commit -m "feat(graphrag): reject approvals whose standard name is not in the closed vocabulary"
```

---

## Task 3: invalid_relation_type 分支回传已知的标准名建议

**Files:**
- Modify: `app/graphrag/normalization.py`
- Test: `tests/graphrag/test_normalization.py`

**Interfaces:**
- 不产生新的对外接口——`normalize_and_write_relations()` 的签名和返回值都不变，只是它在 `invalid_relation_type` 分支调用 `enqueue_for_review()` 时，多传已经算出的 `subject_std`/`object_std` 作为 `suggested_subject_standard_name`/`suggested_object_standard_name`（这两个参数在 `enqueue_for_review()` 里本来就存在，只是这个调用点此前没有传）。

- [ ] **Step 1: 写失败的测试**

把 `tests/graphrag/test_normalization.py` 里的 `test_enqueues_invalid_relation_type_for_review_when_review_conn_provided`（当前第 156-177 行）：

```python
async def test_enqueues_invalid_relation_type_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"}
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "invalid_relation_type"
```

改成：

```python
async def test_enqueues_invalid_relation_type_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"}
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "invalid_relation_type"
    # 两侧实体在这个分支里已经精确对齐过术语表了（只是 relation_type 不
    # 合法才被拦下来），这两个标准名不是"建议"而是已知事实，审核员不该
    # 再重新输入一遍系统已经算出来的正确答案
    assert pending[0]["suggested_subject_standard_name"] == "错误码E502"
    assert pending[0]["suggested_object_standard_name"] == "登录模块"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -k test_enqueues_invalid_relation_type_for_review_when_review_conn_provided -v`
Expected: FAIL——新加的两个断言失败，`suggested_subject_standard_name`/`suggested_object_standard_name` 目前是 `None`。

- [ ] **Step 3: 实现**

把 `app/graphrag/normalization.py` 里 `invalid_relation_type` 分支的 `enqueue_for_review` 调用（当前第 171-181 行）：

```python
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="invalid_relation_type",
                    source=source,
                    tenant_id=tenant_id,
                    evidence=relation.get("evidence", ""),
                )
```

改成：

```python
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="invalid_relation_type",
                    source=source,
                    tenant_id=tenant_id,
                    # 走到这个分支说明两侧都已经精确对齐过术语表了（见函数
                    # 顶部 subject_std/object_std 的计算），只是 relation_type
                    # 不合法——不是"建议"而是已知事实，直接回传，省得审核员
                    # 重新输入系统已经算出来的正确答案
                    suggested_subject_standard_name=subject_std,
                    suggested_object_standard_name=object_std,
                    evidence=relation.get("evidence", ""),
                )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_normalization.py -v`
Expected: PASS，全部用例（包括改动前就有的用例）通过。

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/normalization.py tests/graphrag/test_normalization.py
git commit -m "fix(graphrag): pre-fill known standard names when invalid relation_type triggers review"
```

---

## Task 4: 前端标准名可搜索自动补全

**Files:**
- Create: `frontend/src/admin/termsApi.ts`
- Create: `frontend/src/admin/StandardNameInput.tsx`
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`

**Interfaces:**
- Produces:
  - `termsApi.ts`：`interface GraphTerm { standard_name: string; aliases: string[] }`，`fetchGraphTerms(sessionToken: string): Promise<GraphTerm[]>`。
  - `StandardNameInput.tsx`：`StandardNameInput({ value, onChange, terms, placeholder, ariaLabel }: { value: string; onChange: (value: string) => void; terms: GraphTerm[]; placeholder: string; ariaLabel: string })` —— 受控输入框组件，搜索时同时匹配 `standard_name` 和 `aliases`，下拉选中后只把 `standard_name` 写回 `value`。
- Consumes: Task 1 的 `GET /api/admin/graph-reviews/terms`。

- [ ] **Step 1: 创建 termsApi.ts**

```typescript
import { adminFetch, extractErrorDetail } from './adminApi'

export interface GraphTerm {
  standard_name: string
  aliases: string[]
}

export async function fetchGraphTerms(sessionToken: string): Promise<GraphTerm[]> {
  const response = await adminFetch('/api/admin/graph-reviews/terms', sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: GraphTerm[] }
  return data.terms
}
```

- [ ] **Step 2: 创建 StandardNameInput.tsx**

```tsx
import { useState } from 'react'
import type { GraphTerm } from './termsApi'

interface StandardNameInputProps {
  value: string
  onChange: (value: string) => void
  terms: GraphTerm[]
  placeholder: string
  ariaLabel: string
}

const MAX_SUGGESTIONS = 8

export function StandardNameInput({
  value,
  onChange,
  terms,
  placeholder,
  ariaLabel,
}: StandardNameInputProps) {
  const [isOpen, setIsOpen] = useState(false)

  const query = value.trim()
  const suggestions = query
    ? terms
        .filter(
          (term) =>
            term.standard_name.includes(query) ||
            term.aliases.some((alias) => alias.includes(query)),
        )
        .slice(0, MAX_SUGGESTIONS)
    : []

  return (
    <div className="relative flex-1">
      <input
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
          setIsOpen(true)
        }}
        onFocus={() => setIsOpen(true)}
        onBlur={() => setIsOpen(false)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        className="w-full border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
      />
      {isOpen && suggestions.length > 0 && (
        <ul className="absolute z-10 mt-1 w-full border-2 border-ink bg-paper shadow-brutal-sm">
          {suggestions.map((term) => {
            const matchedAlias = term.standard_name.includes(query)
              ? null
              : term.aliases.find((alias) => alias.includes(query))
            return (
              <li key={term.standard_name}>
                <button
                  type="button"
                  // 鼠标在这里按下时先阻止默认行为，输入框就不会因此失焦——
                  // 不然 input 的 onBlur 会抢在这个按钮的 onClick 之前触发，
                  // 下拉列表在点击生效前就被卸载掉，选不中任何建议
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    onChange(term.standard_name)
                    setIsOpen(false)
                  }}
                  className="block w-full cursor-pointer px-3 py-2 text-left text-sm text-ink hover:bg-card"
                >
                  {term.standard_name}
                  {matchedAlias && (
                    <span className="text-ink-soft">（别名：{matchedAlias}）</span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
```

- [ ] **Step 3: 在 GraphReviewsPage.tsx 里接入**

把顶部 import 区（当前第 1-5 行）：

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { Pager } from './Pager'
```

改成：

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { Pager } from './Pager'
import { StandardNameInput } from './StandardNameInput'
import { fetchGraphTerms, type GraphTerm } from './termsApi'
```

在 `const [processingId, setProcessingId] = useState<number | null>(null)`（当前第 54 行）之后加一行新状态：

```tsx
  const [graphTerms, setGraphTerms] = useState<GraphTerm[]>([])
```

在 `useEffect(() => { document.title = ... }, [])`（当前第 60-62 行）之后加一个新 effect，只在 `sessionToken` 变化时拉取一次（术语表不分租户，不需要跟着 `tenantId` 重新拉）：

```tsx
  useEffect(() => {
    if (!sessionToken) return
    fetchGraphTerms(sessionToken)
      .then(setGraphTerms)
      .catch((err) => {
        console.error('加载术语表失败', err)
      })
  }, [sessionToken])
```

把待审核卡片里的输入框区块（当前第 288-319 行）：

```tsx
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
                aria-label="subject 标准名"
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
                aria-label="object 标准名"
                className="flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
              />
            </div>
```

改成：

```tsx
            <div className="flex gap-3">
              <StandardNameInput
                value={drafts[review.review_id]?.subject ?? ''}
                onChange={(value) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      subject: value,
                    },
                  }))
                }
                terms={graphTerms}
                placeholder="subject 标准名"
                ariaLabel="subject 标准名"
              />
              <StandardNameInput
                value={drafts[review.review_id]?.object ?? ''}
                onChange={(value) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      object: value,
                    },
                  }))
                }
                terms={graphTerms}
                placeholder="object 标准名"
                ariaLabel="object 标准名"
              />
            </div>
```

- [ ] **Step 4: 类型检查 + 构建**

Run（在 `frontend/` 目录下）:
```bash
npm run typecheck
npm run build
```
Expected: 两条命令都无错误退出。

- [ ] **Step 5: 手动验证**

项目没有配置浏览器自动化/前端测试框架，这一步需要人工核对：

1. 确认后端已经跑了 Task 1（`GET /api/admin/graph-reviews/terms` 能正常返回术语表）。
2. 打开"知识图谱审核"页面待审核 tab，点击 subject/object 标准名输入框，在术语表非空的前提下，输入一个术语的完整或部分标准名/别名，确认下拉出现匹配项，别名匹配的项显示"（别名：xxx）"提示。
3. 点击一个下拉建议，确认输入框被填入的是该术语的 `standard_name`（不是刚才搜索用的别名文本）。
4. 确认原有的手动直接输入标准名（不点下拉）仍然可用——自动补全是辅助，不强制通过点击才能填值。
5. 点击"批准"，确认标准名不在术语表里时后端返回 400、页面展示出错误提示（依赖 Task 2 已完成）。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/termsApi.ts frontend/src/admin/StandardNameInput.tsx frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(admin): searchable autocomplete for standard-name inputs, sourced from the term list"
```

---

## Task 5: 批量驳回与批量通过

**Files:**
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`

**Interfaces:**
- Consumes: Task 1-4 已完成的 `graphTerms`/`StandardNameInput`；Task 2 已完成的后端标准名校验（批量通过每一条仍然各自触发这层校验，失败的那条会单独出现在失败清单里）。
- 不新增后端接口——批量驳回/批量通过都是前端循环调用现有的 `POST /{review_id}/approve`、`POST /{review_id}/reject`。

- [ ] **Step 1: 加选中状态、批量提交状态和结果状态**

在 `const [graphTerms, setGraphTerms] = useState<GraphTerm[]>([])`（Task 4 加的那一行）之后加：

```tsx
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [batchRejectNote, setBatchRejectNote] = useState('')
  const [batchProcessing, setBatchProcessing] = useState(false)
  const [batchResult, setBatchResult] = useState<{
    success: number
    failures: { id: number; error: string }[]
  } | null>(null)
```

在 `refreshPending` 的定义之后（当前第 136 行 `}, [sessionToken, tenantId, pendingPage])` 之后）加一个 effect，`pending` 列表变化时清空选中状态和上一次的批量结果——包括翻页、切租户、批量提交完刷新之后：

```tsx
  useEffect(() => {
    setSelectedIds(new Set())
    setBatchResult(null)
  }, [pending])
```

- [ ] **Step 2: 加选中相关的派生状态和切换函数**

在 Step 1 新加的 effect 之后加：

```tsx
  const selectedReviews = pending.filter((review) => selectedIds.has(review.review_id))
  const allOnPageSelected =
    pending.length > 0 && pending.every((review) => selectedIds.has(review.review_id))
  const canBatchApprove =
    selectedReviews.length > 0 &&
    selectedReviews.every(
      (review) => drafts[review.review_id]?.subject && drafts[review.review_id]?.object,
    )

  const toggleSelected = (reviewId: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(reviewId)) {
        next.delete(reviewId)
      } else {
        next.add(reviewId)
      }
      return next
    })
  }

  const toggleSelectAllOnPage = () => {
    setSelectedIds(allOnPageSelected ? new Set() : new Set(pending.map((r) => r.review_id)))
  }
```

- [ ] **Step 3: 加批量提交处理函数**

在 `handleReject` 函数定义结束（当前第 234 行 `}`）之后加：

```tsx
  const handleBatchApprove = async () => {
    if (!sessionToken || batchProcessing || processingId !== null || !canBatchApprove) return
    setBatchProcessing(true)
    setBatchResult(null)
    setError(null)
    const failures: { id: number; error: string }[] = []
    let success = 0
    for (const review of selectedReviews) {
      const draft = drafts[review.review_id]
      try {
        const response = await adminFetch(
          `/api/admin/graph-reviews/${review.review_id}/approve`,
          sessionToken,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              tenant_id: tenantId,
              subject_standard_name: draft.subject,
              object_standard_name: draft.object,
            }),
          },
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '批准失败'))
        }
        success += 1
      } catch (err) {
        failures.push({
          id: review.review_id,
          error: err instanceof Error ? err.message : '批准失败',
        })
      }
    }
    setBatchResult({ success, failures })
    setBatchProcessing(false)
    await refreshPending()
  }

  const handleBatchReject = async () => {
    if (!sessionToken || batchProcessing || processingId !== null) return
    if (selectedReviews.length === 0) return
    setBatchProcessing(true)
    setBatchResult(null)
    setError(null)
    const failures: { id: number; error: string }[] = []
    let success = 0
    for (const review of selectedReviews) {
      try {
        const response = await adminFetch(
          `/api/admin/graph-reviews/${review.review_id}/reject`,
          sessionToken,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tenant_id: tenantId, note: batchRejectNote || null }),
          },
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '驳回失败'))
        }
        success += 1
      } catch (err) {
        failures.push({
          id: review.review_id,
          error: err instanceof Error ? err.message : '驳回失败',
        })
      }
    }
    setBatchResult({ success, failures })
    setBatchRejectNote('')
    setBatchProcessing(false)
    await refreshPending()
  }
```

把 `handleApprove`（当前第 177-206 行）和 `handleReject`（当前第 208-234 行）开头的哨兵检查都加上 `batchProcessing` 判断——`handleApprove` 里的 `if (processingId !== null) return` 改成：

```tsx
    if (processingId !== null || batchProcessing) return
```

`handleReject` 里同样的一行也改成：

```tsx
    if (processingId !== null || batchProcessing) return
```

- [ ] **Step 4: 加选中框和批量操作栏的 UI**

在待审核卡片的 `pending.map(...)` 循环（当前第 271-353 行）**之前**，紧跟在 `{tab === 'pending' && !pendingLoaded && <p className="text-ink-soft">加载中…</p>}`（当前第 270 行）之后插入一个全选行：

```tsx
      {tab === 'pending' && pendingLoaded && pending.length > 0 && (
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={allOnPageSelected}
            onChange={toggleSelectAllOnPage}
            aria-label="全选本页"
          />
          全选本页（{pending.length} 条）
        </label>
      )}
```

在每张待审核卡片最外层 `<div>`（当前第 274-277 行）的开头，`候选：...` 那个 `<p>` 之前，加一个选中框：

```tsx
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                type="checkbox"
                checked={selectedIds.has(review.review_id)}
                onChange={() => toggleSelected(review.review_id)}
                aria-label={`选中候选 ${review.review_id}`}
              />
              选中批量处理
            </label>
```

在待审核卡片循环结束、分页器之前（当前第 354-363 行 `{tab === 'pending' && pendingLoaded && pending.length === 0 && ...}` 和 Pager 那两块之前）插入批量操作栏，只在有选中项时显示：

```tsx
      {tab === 'pending' && selectedReviews.length > 0 && (
        <div className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
          <p className="text-sm font-bold text-ink">已选中 {selectedReviews.length} 条</p>
          <textarea
            value={batchRejectNote}
            onChange={(event) => setBatchRejectNote(event.target.value)}
            placeholder="批量驳回备注（可选，应用到本次选中的所有记录）"
            aria-label="批量驳回备注"
            rows={2}
            className="border-2 border-ink bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <div className="flex gap-3">
            <button
              type="button"
              onClick={handleBatchApprove}
              disabled={!canBatchApprove || batchProcessing || processingId !== null}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {batchProcessing ? '批量处理中…' : '批量通过'}
            </button>
            <button
              type="button"
              onClick={handleBatchReject}
              disabled={batchProcessing || processingId !== null}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {batchProcessing ? '批量处理中…' : '批量驳回'}
            </button>
          </div>
          {!canBatchApprove && (
            <p className="text-xs text-ink-soft">
              批量通过要求选中的每一条都已填好 subject/object 标准名。
            </p>
          )}
          {batchResult && (
            <p className="text-sm text-ink">
              成功 {batchResult.success} 条
              {batchResult.failures.length > 0 &&
                `，失败 ${batchResult.failures.length} 条：${batchResult.failures
                  .map((f) => `#${f.id} ${f.error}`)
                  .join('；')}`}
            </p>
          )}
        </div>
      )}
```

- [ ] **Step 5: 类型检查 + 构建**

Run（在 `frontend/` 目录下）:
```bash
npm run typecheck
npm run build
```
Expected: 两条命令都无错误退出。

- [ ] **Step 6: 手动验证**

1. 打开待审核 tab，勾选"全选本页"，确认本页所有卡片的选中框一起打勾；再点一次，确认全部取消。
2. 勾选两条，只给其中一条填好 subject/object 标准名，确认"批量通过"按钮保持禁用、并显示"批量通过要求选中的每一条都已填好…"提示；把另一条也填好后按钮变为可点。
3. 点击"批量通过"，确认两条都被批准、从待审核列表消失、切到历史记录能看到；确认页面下方出现"成功 2 条"的汇总。
4. 重新勾选若干条，填入批量驳回备注，点击"批量驳回"，确认这些记录都进了历史记录、`resolved_note` 是刚才填的备注。
5. 故意让批量提交里有一条会失败（比如提前手动驳回其中一条，再把它和别的一起选中批量通过/驳回），确认失败汇总里正确列出了失败的 review_id 和原因，其它条目仍然成功。
6. 翻页，确认选中状态被清空（不残留上一页的勾选）。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(admin): batch approve and batch reject for the pending graph review queue"
```

---

## 全部任务完成后

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了已知无关的 `test_returns_none_when_tts_not_configured` 之外全部通过。

Run（`frontend/` 目录下）: `npm run typecheck && npm run build`
Expected: 均无错误退出。
