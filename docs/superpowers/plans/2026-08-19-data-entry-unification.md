# 三渠道数据填充统一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把实体/关系的三条写入渠道（手工、ETL、知识图谱审核）从彼此割裂、部分绕开本体 schema 约束的状态，改造成一套一致、可追溯、真正受本体 schema 约束的数据填充体系。

**Architecture:** 后端分三条独立改造线——① `terms` 表加 `source` 溯源列并收窄创建入口；② 审核队列表扩展类型候选字段；③ LLM 抽取管线（prompt 动态拼确认 schema + 写入侧新增确认范围校验 + 未确认 schema 时跳过抽取）。前端把三个平级页面合并进「数据填充」一个侧边栏入口下的三个可深链接子 tab，「实体列表」（原术语库管理）收窄为浏览维护，「非结构化数据加工"（原知识图谱审核）新增内联创建实体能力。

**Tech Stack:** FastAPI + aiosqlite（后端）、React + TypeScript + React Router（前端）、Neo4j（图谱存储）。

**Spec:** `docs/superpowers/specs/2026-08-19-data-entry-unification-design.md`

## Global Constraints

- `source` 语义：只记录**创建时**的渠道（manual/etl/review/unknown），后续人工编辑（`update_term`）不改变已有 `source` 值。
- `create_term()` 新增 `source: str = "manual"` 参数（有默认值，不强制改动测试里现存的 45 处调用——这是本计划的显式选择，见 Task 1 说明）；`upsert_term_with_node_key()` 新增 `source: str = "etl"` 参数（同样默认值，ETL 调用点不需要改动）。
- `normalize_and_write_relations()` 新增 `confirmed_relation_types: set[str]` 和 `allowed_combinations: set[tuple[str, str, str]]` 两个**必填**关键字参数（无默认值）——这是本计划里唯一强制要求改动全部既有调用点/测试的函数，因为这正是"写入侧新增确认范围校验"这个核心能力本身。
- 前端子 tab 命名：「实体列表」（原手工录入/术语库管理）、「结构化数据加工」（原 ETL 跑批）、「非结构化数据加工」（原知识图谱审核）；侧边栏入口命名「数据填充」。
- 内联创建实体表单：只要求 `term_type`（下拉限定 `status="confirmed"`）+ `product_line` 必填，`aliases`/`extra_properties` 留空。
- 旧路由 `/admin/terms`、`/admin/graph-reviews`、`/admin/schema-etl` 必须重定向到新路径，不能变成 404。

---

## Task 1: `terms` 表加 source 溯源列，terms_store.py 全线接入

**Files:**
- Modify: `app/graphrag/ontology.py`（`Term` dataclass 加 `source` 字段）
- Modify: `app/graphrag/terms_store.py`（`_SCHEMA_SQL`、`ensure_terms_schema`、`_row_to_term`、`list_terms`、`create_term`、`upsert_term_with_node_key`）
- Test: `tests/graphrag/test_terms_store.py`

**Interfaces:**
- Consumes：`app.db_migrations.add_column_if_missing`（已有，terms_store.py 已导入）。
- Produces：`Term.source: str`（新字段）；`create_term(..., source: str = "manual")`；`upsert_term_with_node_key(..., source: str = "etl")`；`list_terms(conn, tenant_id, *, limit=None, offset=0, source: str | None = None)`——`source=None`（默认）不过滤，返回全部；传具体值只返回该来源的行。

- [ ] **Step 1: `Term` dataclass 加 `source` 字段**

`app/graphrag/ontology.py` 第 10-17 行的 `Term` dataclass，在 `extra_properties` 之后加一个有默认值的字段（放在最后，默认值保证任何位置参数调用方式的既有代码不受影响）：

```python
@dataclass
class Term:
    tenant_id: str
    node_key: str
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str | int | float | list[float]] = field(default_factory=dict)
    source: str = "unknown"
```

- [ ] **Step 2: `terms` 表建表 SQL 和迁移加 `source` 列**

`app/graphrag/terms_store.py` 第 19-32 行的 `_SCHEMA_SQL`，加一行：

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    tenant_id         TEXT NOT NULL,
    node_key          TEXT NOT NULL,
    standard_name     TEXT NOT NULL,
    aliases           TEXT NOT NULL,
    term_type         TEXT NOT NULL,
    product_line      TEXT NOT NULL,
    extra_properties  TEXT NOT NULL DEFAULT '{}',
    source            TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (tenant_id, node_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name
    ON terms(tenant_id, standard_name);
"""
```

`ensure_terms_schema`（第 104-133 行）里紧跟着现有的 `extra_properties` 迁移那行加一句（`_SCHEMA_SQL` 里的 `CREATE TABLE IF NOT EXISTS` 只对全新表生效，已存在的表要靠 `add_column_if_missing` 补列，`DEFAULT 'unknown'` 会让 SQLite 自动给所有历史行回填这个值——这正是 spec 决策 C.3 要求的"历史数据回填为 unknown"，不需要额外写迁移逻辑）：

```python
    if table_already_existed:
        await add_column_if_missing(
            conn, table="terms", column="extra_properties", ddl="TEXT NOT NULL DEFAULT '{}'"
        )
        await add_column_if_missing(
            conn, table="terms", column="source", ddl="TEXT NOT NULL DEFAULT 'unknown'"
        )
        await _migrate_terms_table_to_tenant_scoped_if_needed(conn)
```

- [ ] **Step 3: `_row_to_term` 读出 source**

`app/graphrag/terms_store.py` 第 217-226 行：

```python
def _row_to_term(row: aiosqlite.Row) -> Term:
    return Term(
        tenant_id=row["tenant_id"],
        node_key=row["node_key"],
        standard_name=row["standard_name"],
        aliases=json.loads(row["aliases"]),
        term_type=row["term_type"],
        product_line=row["product_line"],
        extra_properties=json.loads(row["extra_properties"]),
        source=row["source"],
    )
```

- [ ] **Step 4: `list_terms`/`get_term` 的 SELECT 加 source 列，`list_terms` 加可选 source 过滤**

`app/graphrag/terms_store.py` 第 229-246 行的 `list_terms`：

```python
async def list_terms(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    source: str | None = None,
) -> list[Term]:
    """limit=None（默认）返回该租户全部术语……（原有说明不变）

    source=None（默认）不按来源过滤；传具体值（manual/etl/review/unknown）
    只返回该来源的行，供"实体列表"页的来源筛选用。
    """
    conn.row_factory = aiosqlite.Row
    if source is None:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
            "extra_properties, source FROM terms WHERE tenant_id = ? "
            "ORDER BY standard_name LIMIT ? OFFSET ?",
            (tenant_id, limit if limit is not None else -1, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, product_line, "
            "extra_properties, source FROM terms WHERE tenant_id = ? AND source = ? "
            "ORDER BY standard_name LIMIT ? OFFSET ?",
            (tenant_id, source, limit if limit is not None else -1, offset),
        )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]
```

`count_terms`（第 249-252 行）同样加 `source: str | None = None` 可选参数，SQL 加对应的 `AND source = ?` 分支（管理后台分页需要总数与筛选后的列表总数一致）：

```python
async def count_terms(
    conn: aiosqlite.Connection, tenant_id: str, *, source: str | None = None
) -> int:
    if source is None:
        cursor = await conn.execute("SELECT COUNT(*) FROM terms WHERE tenant_id = ?", (tenant_id,))
        row = await cursor.fetchone()
        return row[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE tenant_id = ? AND source = ?", (tenant_id, source)
    )
    row = await cursor.fetchone()
    return row[0]
```

`get_term`（第 255-265 行）的 SELECT 语句同样加 `, source`。

- [ ] **Step 5: `create_term` 加 `source` 参数并写入**

`app/graphrag/terms_store.py` 第 336-370 行：

```python
async def create_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, object] | None = None,
    source: str = "manual",
) -> None:
    """node_key 创建时直接取 standard_name 的值……（原有说明不变）

    source 记录这条术语最初是通过哪个渠道创建的（manual/etl/review），
    默认值 "manual" 只是为了不用逐个改动测试里大量既有的 create_term()
    调用——本计划里唯一真正的生产调用点是 admin_terms_routes.py 的
    create_new_term，它现在只会被"知识图谱审核"页的内联创建调用，会显式
    传 source="review"。见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 C。
    """
    extra_properties = extra_properties or {}
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type, product_line=product_line,
        extra_properties=extra_properties,
    )
    await _check_name_conflict(conn, tenant_id=tenant_id, standard_name=standard_name, aliases=aliases)
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "product_line, extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                standard_name,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
                source,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{standard_name!r} 已经是已有术语的标准名，不能重复创建")
    await conn.commit()
```

- [ ] **Step 6: `update_term` 不碰 source 列**

`app/graphrag/terms_store.py` 第 373-416 行的 `update_term` **不需要任何改动**——它的 UPDATE 语句本来就不包含 `source` 列，这正是 spec 决策 C.4 要求的行为（人工编辑不改变已有 source）。只在函数 docstring 末尾补一句说明，防止未来有人"顺手"把 source 加进 UPDATE 语句：

```python
    """standard_name 是当前（改名前）的名字……（原有说明不变）

    UPDATE 语句不写 source 列——这是刻意的：source 只记录创建时的渠道，
    人工编辑（无论改名、改别名还是改属性）都不改变它，见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 C.4。
    """
```

- [ ] **Step 7: `upsert_term_with_node_key` 加 `source` 参数**

`app/graphrag/terms_store.py` 第 443-505 行：

```python
async def upsert_term_with_node_key(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, object] | None = None,
    source: str = "etl",
) -> None:
    """ETL 专用的幂等写入……（原有说明不变）

    source 默认值 "etl"——这个函数目前唯一的生产调用点就是
    schema_etl.py::_write_entity_mapping，不需要显式传参也总是正确的。
    """
    extra_properties = extra_properties or {}
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT extra_properties FROM terms WHERE tenant_id = ? AND node_key = ?",
        (tenant_id, node_key),
    )
    existing_row = await cursor.fetchone()
    existing_extra_property_keys = (
        frozenset(json.loads(existing_row["extra_properties"]))
        if existing_row is not None else frozenset()
    )
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type, product_line=product_line,
        extra_properties=extra_properties,
        existing_extra_property_keys=existing_extra_property_keys,
    )
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "product_line, extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, node_key) DO UPDATE SET "
            "standard_name = excluded.standard_name, aliases = excluded.aliases, "
            "term_type = excluded.term_type, product_line = excluded.product_line, "
            "extra_properties = excluded.extra_properties",
            (
                tenant_id,
                node_key,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
                source,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(
            f"{standard_name!r} 已经是租户 {tenant_id!r} 下另一个术语的标准名，无法写入"
        )
    await conn.commit()
```

注意：`ON CONFLICT ... DO UPDATE SET` 故意不包含 `source = excluded.source`——已存在的行（哪怕是被 ETL 再次 upsert）保留它最初的 source，这与 `update_term` 不碰 source 列是同一个道理的两种写法（这里是 upsert 语句层面的对应处理）。

- [ ] **Step 8: 新增测试**

在 `tests/graphrag/test_terms_store.py` 里新增（放在文件里其它 `create_term`/`upsert_term_with_node_key` 相关测试附近）：

```python
async def test_create_term_defaults_source_to_manual(tmp_path):
    conn = await _open_test_db(tmp_path)  # 沿用本文件已有的测试库初始化 helper
    await create_term(
        conn, tenant_id="t1", standard_name="term-a", aliases=[],
        term_type="module", product_line="核心平台",
    )
    term = await get_term(conn, "t1", "term-a")
    assert term.source == "manual"


async def test_create_term_explicit_source(tmp_path):
    conn = await _open_test_db(tmp_path)
    await create_term(
        conn, tenant_id="t1", standard_name="term-b", aliases=[],
        term_type="module", product_line="核心平台", source="review",
    )
    term = await get_term(conn, "t1", "term-b")
    assert term.source == "review"


async def test_upsert_term_with_node_key_defaults_source_to_etl(tmp_path):
    conn = await _open_test_db(tmp_path)
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="k1", standard_name="term-c", aliases=[],
        term_type="module", product_line="核心平台",
    )
    term = await get_term(conn, "t1", "term-c")
    assert term.source == "etl"


async def test_update_term_does_not_change_source(tmp_path):
    conn = await _open_test_db(tmp_path)
    await create_term(
        conn, tenant_id="t1", standard_name="term-d", aliases=[],
        term_type="module", product_line="核心平台", source="etl",
    )
    await update_term(
        conn, tenant_id="t1", standard_name="term-d", new_standard_name="term-d-renamed",
        aliases=["alias"], term_type="module", product_line="核心平台",
    )
    term = await get_term(conn, "t1", "term-d-renamed")
    assert term.source == "etl"


async def test_list_terms_filters_by_source(tmp_path):
    conn = await _open_test_db(tmp_path)
    await create_term(
        conn, tenant_id="t1", standard_name="m1", aliases=[],
        term_type="module", product_line="核心平台", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="e1", aliases=[],
        term_type="module", product_line="核心平台", source="etl",
    )
    manual_only = await list_terms(conn, "t1", source="manual")
    assert [t.standard_name for t in manual_only] == ["m1"]
```

用本文件已有的测试库初始化方式替换 `_open_test_db(tmp_path)` 这个占位调用——打开本文件看顶部现有测试怎么建库/建连接，照抄那个模式，不要引入新的 helper 名字。

- [ ] **Step 9: 运行测试**

Run: `python -m pytest tests/graphrag/test_terms_store.py -v`
Expected: 全部 PASS，包括新增的 5 个用例。

- [ ] **Step 10: Commit**

```bash
git add app/graphrag/ontology.py app/graphrag/terms_store.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): add source provenance column to terms table"
```

---

## Task 2: admin terms 路由与前端 API 客户端贯通 source 字段

**Files:**
- Modify: `app/api/admin_terms_routes.py`
- Modify: `frontend/src/admin/termsApi.ts`
- Test: `tests/api/test_admin_terms_routes.py`

**Interfaces:**
- Consumes：Task 1 的 `Term.source`、`create_term(..., source=...)`、`list_terms(..., source=...)`、`count_terms(..., source=...)`。
- Produces：`GET /api/admin/{tenant_id}/terms` 新增可选 query 参数 `source`；`TermResponse`/`TermWriteRequest` 新增 `source` 字段；前端 `TermRecord` 类型新增 `source: string`，`fetchTermsPage` 新增可选 `source` 参数。

- [ ] **Step 1: `TermResponse`/`TermWriteRequest` 加 `source` 字段**

`app/api/admin_terms_routes.py` 第 33-38 行：

```python
class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, Any] = {}
    source: str
```

第 46-51 行的 `TermWriteRequest`（这是创建/更新共用的请求体；更新场景下这个字段会被忽略——见 Step 3 说明）：

```python
class TermWriteRequest(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, Any] = {}
    source: str = "manual"
```

- [ ] **Step 2: `_to_response` 透传 source**

`app/api/admin_terms_routes.py` 第 77-84 行：

```python
def _to_response(term: Term) -> TermResponse:
    return TermResponse(
        standard_name=term.standard_name,
        aliases=term.aliases,
        term_type=term.term_type,
        product_line=term.product_line,
        extra_properties=term.extra_properties,
        source=term.source,
    )
```

- [ ] **Step 3: `list_all_terms` 加 source 过滤 query 参数**

`app/api/admin_terms_routes.py` 第 87-108 行：

```python
@router.get("", response_model=TermListResponse)
async def list_all_terms(
    tenant_id: str,
    page: int | None = None,
    page_size: int | None = None,
    source: str | None = None,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> TermListResponse:
    if page is None and page_size is None:
        terms = await list_terms(review_conn, tenant_id, source=source)
    else:
        effective_page = page or 1
        effective_page_size = page_size or 20
        offset = (effective_page - 1) * effective_page_size
        terms = await list_terms(
            review_conn, tenant_id, limit=effective_page_size, offset=offset, source=source
        )
    total = await count_terms(review_conn, tenant_id, source=source)
    return TermListResponse(terms=[_to_response(term) for term in terms], total=total)
```

- [ ] **Step 4: `create_new_term` 透传 payload.source**

`app/api/admin_terms_routes.py` 第 111-156 行的 `create_new_term`，`create_term(...)` 调用加一个关键字参数：

```python
        await create_term(
            review_conn,
            tenant_id=tenant_id,
            standard_name=payload.standard_name,
            aliases=payload.aliases,
            term_type=payload.term_type,
            product_line=payload.product_line,
            extra_properties=payload.extra_properties,
            source=payload.source,
        )
```

`term = Term(...)` 构造那几行（第 138-146 行）也加 `source=payload.source`。

- [ ] **Step 5: `update_existing_term` 里的 `Term(...)` 构造补 source**

`update_existing_term`（第 159-227 行）里两处 `Term(...)` 构造（第 210-218 行那处，用于图谱同步）需要补 `source=existing_before_update.source`——这里不是数据库层面的问题（`update_term` 本来就不改 source，第 391 行的说明已经解释过），只是这个函数体内临时构造的 `Term` 对象要跟数据库里实际的值保持一致，避免传给 `graph_client.sync_term` 的对象字段不完整（虽然 Neo4j 侧不消费 source，但保持 Term 对象语义完整是好习惯，也方便未来任何读这个变量的新代码不会读到错误的默认值）：

```python
    term = Term(
        tenant_id=tenant_id,
        node_key=node_key,
        standard_name=payload.standard_name,
        aliases=payload.aliases,
        term_type=payload.term_type,
        product_line=payload.product_line,
        extra_properties=payload.extra_properties,
        source=existing_before_update.source,
    )
```

- [ ] **Step 6: 前端 `termsApi.ts` 类型与函数更新**

`frontend/src/admin/termsApi.ts`：

```typescript
export interface TermRecord extends GraphTerm {
  term_type: string
  product_line: string
  source: string
}
```

`fetchTermsPage` 加可选 `source` 参数：

```typescript
export async function fetchTermsPage(
  sessionToken: string,
  tenantId: string,
  page: number,
  pageSize: number,
  source?: string,
): Promise<TermPage> {
  const sourceParam = source ? `&source=${encodeURIComponent(source)}` : ''
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms?page=${page}&page_size=${pageSize}${sourceParam}`,
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

`createTerm` 的调用方（Task 9 的 GraphReviewsPage 内联创建）会在传入的 `term` 对象里显式带 `source: 'review'`，`createTerm`/`updateTerm` 函数本身不需要改动（`TermRecord` 类型已经包含 `source`，JSON 序列化会自动带上）。

- [ ] **Step 7: 更新/新增测试**

`tests/api/test_admin_terms_routes.py` 里所有构造 `TermWriteRequest`/断言 `TermResponse` 的地方，如果直接用 dict 字面量发请求体（而不是 Pydantic 模型），不受影响（`source` 有默认值）；如果测试断言了完整的响应体 JSON（比如 `assert response.json() == {...}`），需要在期望的 dict 里补上 `"source": "manual"`（或该测试场景下的实际值）。新增：

```python
async def test_create_term_returns_source(...):
    # 复用本文件现有的测试客户端/夹具模式
    response = await client.post(
        f"/api/admin/{tenant_id}/terms",
        json={
            "standard_name": "term-x", "aliases": [], "term_type": "module",
            "product_line": "核心平台", "source": "review",
        },
    )
    assert response.status_code == 200
    assert response.json()["source"] == "review"


async def test_list_terms_filters_by_source_query_param(...):
    # 先分别以 source=manual 和 source=etl 创建两条，再用 ?source=etl 查询
    # 断言只返回 etl 那一条
    ...
```

（用本文件已有的测试客户端搭建方式补全，不要引入新的夹具。）

- [ ] **Step 8: 运行测试**

Run: `python -m pytest tests/api/test_admin_terms_routes.py -v`
Expected: 全部 PASS。

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误（`TermRecord` 新增必填字段 `source` 后，检查 `TermsPage.tsx`/`GraphReviewsPage.tsx` 里任何手写构造 `TermRecord` 字面量的地方是否需要补这个字段——本任务不改这两个页面的业务逻辑，只需要确认类型检查通过；如果某处报错缺 `source` 字段，是 Task 3/Task 9 要做的事，本任务里可以先给该字面量补一个占位的 `source: 'manual'` 让类型检查通过，Task 3/9 会替换成真实来源）。

- [ ] **Step 9: Commit**

```bash
git add app/api/admin_terms_routes.py frontend/src/admin/termsApi.ts tests/api/test_admin_terms_routes.py
git commit -m "feat(api): expose source provenance on terms endpoints"
```

---

## Task 3: 「实体列表」页去掉创建入口，加来源标签+筛选

**Files:**
- Modify: `frontend/src/admin/TermsPage.tsx`

**Interfaces:**
- Consumes：Task 2 的 `TermRecord.source`、`fetchTermsPage(..., source?)`。
- Produces：无新增导出（页面组件本身，供 Task 10 挂载到新路由）。

- [ ] **Step 1: 删除创建表单相关的 state 和 UI**

`frontend/src/admin/TermsPage.tsx` 删除：
- `newDraft`/`setNewDraft`/`creating`/`setCreating` 这几个 state（第 56-57 行）。
- `handleCreate` 函数（第 130-144 行）。
- `emptyDraft` 常量（第 41 行，如果删完 `newDraft` 后没有别处引用就一并删除；`toDraft`/`draftToRecord` 继续保留，编辑功能还要用）。
- JSX 里"新增术语"整个卡片区块（第 193-251 行）。
- `import { createTerm, ... }`（第 4 行）里的 `createTerm` 不再被这个文件使用，从 import 列表里删掉（`deleteTerm`/`fetchTermsPage`/`updateTerm`/`TermRecord` 继续保留）。

- [ ] **Step 2: 新增来源筛选 state 和请求参数联动**

```typescript
type SourceFilter = 'all' | 'manual' | 'etl' | 'review' | 'unknown'

const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all')
```

`refresh`（第 96-112 行）的 `fetchTermsPage` 调用加上 source 参数，并把 `sourceFilter` 加入 `useCallback` 依赖数组：

```typescript
  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const requestId = ++refreshRequestIdRef.current
    try {
      const data = await fetchTermsPage(
        sessionToken, tenantId, page, PAGE_SIZE,
        sourceFilter === 'all' ? undefined : sourceFilter,
      )
      if (requestId !== refreshRequestIdRef.current) return
      setTerms(data.terms)
      setTotal(data.total)
    } catch (err) {
      if (requestId !== refreshRequestIdRef.current) return
      setError(err instanceof Error ? err.message : '加载术语表失败')
    } finally {
      if (requestId === refreshRequestIdRef.current) {
        setLoaded(true)
      }
    }
  }, [sessionToken, tenantId, page, sourceFilter])
```

切换筛选条件时页码回到第一页（照抄现有"切租户回到第一页"的 effect 模式，第 120-122 行）：

```typescript
  useEffect(() => {
    setPage(1)
  }, [sourceFilter])
```

- [ ] **Step 3: 加筛选下拉 UI**

在 `<h1>` 标题下方新增（标题文案同时改成"实体列表"）：

```tsx
      <h1 className="text-xl font-bold text-ink">实体列表</h1>

      <div className="flex items-center gap-2">
        <label htmlFor="source-filter" className="text-sm font-bold text-ink">
          来源
        </label>
        <select
          id="source-filter"
          value={sourceFilter}
          onChange={(event) => setSourceFilter(event.target.value as SourceFilter)}
          className="border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
        >
          <option value="all">全部</option>
          <option value="manual">手工</option>
          <option value="etl">ETL</option>
          <option value="review">知识图谱审核</option>
          <option value="unknown">未知（历史数据）</option>
        </select>
      </div>
```

- [ ] **Step 4: 列表每一项加来源标签**

在每条术语展示行（第 271-282 行区域）的类型/产品线文字旁边加一个小标签：

```tsx
                  <span className="text-ink-soft">
                    {' '}
                    · {term.term_type || '（无类型）'} · {term.product_line || '（无产品线）'}
                  </span>
                  <span className="ml-2 border border-ink-soft px-1.5 py-0.5 text-xs text-ink-soft">
                    来源：{
                      { manual: '手工', etl: 'ETL', review: '知识图谱审核', unknown: '未知' }[
                        term.source
                      ] ?? term.source
                    }
                  </span>
```

- [ ] **Step 5: 空列表提示文案调整**

第 388-390 行"还没有任何术语，用上面的表单新增一个"这句话不再成立（已经没有表单了），改成：

```tsx
      {loaded && !error && terms.length === 0 && (
        <p className="text-ink-soft">
          还没有任何实体。实体创建只能通过「结构化数据加工」（ETL）或「非结构化数据加工」（知识图谱审核）完成。
        </p>
      )}
```

- [ ] **Step 6: 手动验证**

启动前端开发服务器，打开实体列表页：
1. 确认页面不再有"新增术语"表单。
2. 确认每条术语显示来源标签。
3. 切换来源筛选下拉，确认列表随之刷新且筛选结果正确。
4. 确认编辑/删除功能仍然正常工作（这两块代码本任务未改动）。

- [ ] **Step 7: 运行测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 8: Commit**

```bash
git add frontend/src/admin/TermsPage.tsx
git commit -m "feat(frontend): trim entity list page to browse-only with source filter"
```

---

## Task 4: 审核队列表加候选类型字段

**Files:**
- Modify: `app/graphrag/review_queue.py`

**Interfaces:**
- Consumes：无新增外部依赖。
- Produces：`graph_review_queue` 表新增 `subject_type_candidate`/`object_type_candidate` 列（nullable）；`enqueue_for_review(..., subject_type_candidate: str | None = None, object_type_candidate: str | None = None)`；`list_pending_reviews`/`list_resolved_reviews` 返回的 dict 新增这两个键。

- [ ] **Step 1: 建表迁移加两列**

`app/graphrag/review_queue.py` 的 `ensure_review_schema`（第 63-106 行），紧跟着现有的 `evidence` 列迁移之后加：

```python
    # subject_type_candidate/object_type_candidate 记录 LLM 抽取阶段给出的
    # 候选实体类型（已经是从该租户已确认 term_type 集合里选出的合法值，见
    # llm_extractor.py）——供审核页内联创建实体时预填表单的 term_type 下拉。
    # 历史候选行没有这个信息，回填 NULL（不是空字符串：空字符串会被
    # 内联创建表单误当成"选中了一个空选项"，NULL 更准确地表达"未知/不适用"）。
    await add_column_if_missing(
        conn, table="graph_review_queue", column="subject_type_candidate", ddl="TEXT",
    )
    await add_column_if_missing(
        conn, table="graph_review_queue", column="object_type_candidate", ddl="TEXT",
    )
```

- [ ] **Step 2: `enqueue_for_review` 加两个可选参数**

`app/graphrag/review_queue.py` 第 109-151 行：

```python
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
    evidence: str = "",
    subject_type_candidate: str | None = None,
    object_type_candidate: str | None = None,
) -> int:
    """把未能对齐术语表（或关系类型不合法）的候选关系存入待审核队列，返回 review_id。

    ……（原有说明不变）

    subject_type_candidate/object_type_candidate 是 LLM 抽取阶段给出的候选
    实体类型（见 llm_extractor.py 的动态 prompt 改造），默认 None——不是所有
    调用方都一定拿得到（比如未来可能有的非 LLM 抽取来源），不强制要求。
    """
    cursor = await conn.execute(
        "INSERT INTO graph_review_queue "
        "(subject_candidate, object_candidate, relation_type, reason, "
        "suggested_subject_standard_name, suggested_object_standard_name, "
        "source, tenant_id, evidence, subject_type_candidate, object_type_candidate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject_candidate,
            object_candidate,
            relation_type,
            reason,
            suggested_subject_standard_name,
            suggested_object_standard_name,
            source,
            tenant_id,
            evidence,
            subject_type_candidate,
            object_type_candidate,
        ),
    )
    await conn.commit()
    return cursor.lastrowid
```

- [ ] **Step 3: `list_pending_reviews`/`list_resolved_reviews` 的 SELECT 加两列**

`app/graphrag/review_queue.py` 第 154-175 行的 `list_pending_reviews`，SELECT 子句加 `subject_type_candidate, object_type_candidate`：

```python
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "source, evidence, created_at, subject_type_candidate, object_type_candidate "
        "FROM graph_review_queue "
        "WHERE status = 'pending' AND tenant_id = ? ORDER BY review_id LIMIT ? OFFSET ?",
        (tenant_id, limit if limit is not None else -1, offset),
    )
```

`list_resolved_reviews`（第 178-211 行）两个分支的 SELECT 子句同样加 `, subject_type_candidate, object_type_candidate`。

- [ ] **Step 4: 新增/更新测试**

`tests/graphrag/test_review_queue.py` 里找到已有的 `enqueue_for_review`/`list_pending_reviews` 相关测试，新增一个覆盖新字段的用例（照抄文件里已有测试的库初始化模式）：

```python
async def test_enqueue_for_review_stores_type_candidates(...):
    review_id = await enqueue_for_review(
        conn, subject_candidate="错误码E509", object_candidate="重启路由器",
        relation_type="ADDRESSED_BY", reason="subject_unresolved",
        source="a.md", tenant_id="t1",
        subject_type_candidate="error_code", object_type_candidate="solution",
    )
    pending = await list_pending_reviews(conn, tenant_id="t1")
    row = next(r for r in pending if r["review_id"] == review_id)
    assert row["subject_type_candidate"] == "error_code"
    assert row["object_type_candidate"] == "solution"
```

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/graphrag/test_review_queue.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/review_queue.py tests/graphrag/test_review_queue.py
git commit -m "feat(graphrag): store LLM-suggested term types on review candidates"
```

---

## Task 5: LLM 抽取 prompt 按租户已确认本体动态拼接

**Files:**
- Modify: `app/graphrag/llm_extractor.py`
- Test: `tests/graphrag/test_llm_extractor.py`

**Interfaces:**
- Consumes：`app.graphrag.ontology_relations.RelationType`（或等价的 value 字符串列表）、`app.graphrag.ontology_categories.TermTypeCategory`（或等价字符串列表）、`app.graphrag.ontology_constraints.AllowedCombination`。
- Produces：`extract_candidate_relations(segments, *, llm_registry, llm_provider_name, relation_types: list[str], term_types: list[str], allowed_combinations: list[AllowedCombination], timeout_sec=30.0) -> list[dict[str, str]]`——返回的每个 dict 新增 `subject_type`/`object_type` 两个键。

- [ ] **Step 1: system prompt 改为函数动态生成**

`app/graphrag/llm_extractor.py` 把第 16-47 行的模块级常量 `_SYSTEM_PROMPT` 换成一个构建函数，动态拼接该租户已确认的关系类型/实体类型/允许组合，而不是硬编码 10 种通用关系：

```python
from app.graphrag.ontology_constraints import AllowedCombination

_SYSTEM_PROMPT_TEMPLATE = (
    "你是知识图谱关系抽取器。"
    "请从给定文档片段中抽取专有名词之间的关系，但只能抽取下面明确列出的"
    "实体类型和关系类型，不能抽取范围之外的内容——这是这个租户在本体 "
    "schema 里已经确认好的封闭定义，不是建议，是硬约束。\n"
    "允许的实体类型（subject_type/object_type 只能是这些值之一）：\n"
    "{term_types}\n"
    "允许的关系类型（relation_type 只能是这些值之一）：\n"
    "{relation_types}\n"
    "允许的（主体类型, 关系类型, 客体类型）三元组组合，subject_type/"
    "relation_type/object_type 的组合必须命中下面某一行，命中不了就不要"
    "输出这条关系：\n"
    "{allowed_combinations}\n"
    '只输出 JSON：{{"relations":[{{"subject":"...","subject_type":"...",'
    '"object":"...","object_type":"...","relation_type":"...",'
    '"evidence":"..."}}]}}。subject_type/object_type 分别是 subject/object '
    "这两个专有名词各自的实体类型，必须是上面允许的实体类型之一。"
    "evidence 是原文里支持这条关系的一句话原文摘录，给人工审核用，必须是"
    "原文摘录、不能改写或概括；实在找不到能直接引用的完整单句时，摘取最"
    "贴近的一小段原文，不要留空。"
    "不确定的内容不要编造，抽不出符合上述范围的关系就返回空列表。"
    "如果输入包含多个用 [片段N] 标记分隔的片段，只抽取同一个片段内部出现的"
    "关系，不要把不同片段里的实体强行关联起来。"
)


def _build_system_prompt(
    *, relation_types: list[str], term_types: list[str],
    allowed_combinations: list[AllowedCombination],
) -> str:
    combos_text = "\n".join(
        f"- {c.subject_term_type} {c.relation_type} {c.object_term_type}"
        for c in allowed_combinations
    ) or "（无——该租户尚未配置任何允许组合，本次不会抽取出任何关系）"
    return _SYSTEM_PROMPT_TEMPLATE.format(
        term_types="、".join(term_types) or "（无）",
        relation_types="、".join(relation_types) or "（无）",
        allowed_combinations=combos_text,
    )
```

这里删掉了原 prompt 里那段举例说明每种通用关系类型语义的文字（"RELATED_TO（兜底弱关联，如……）"等 10 行）——那些例子是为通用/固定类型服务的，现在类型集合是每个租户各不相同的动态值，没有对应的固定例子可以给；`_build_system_prompt` 用允许组合列表本身（`subject_type relation_type object_type` 三元组）替代举例的作用，让 LLM 知道具体哪些组合合法。

- [ ] **Step 2: `extract_candidate_relations` 签名加三个新参数，调用新的 prompt 构建函数**

`app/graphrag/llm_extractor.py` 第 62-82 行：

```python
async def extract_candidate_relations(
    segments: list[str],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    relation_types: list[str],
    term_types: list[str],
    allowed_combinations: list[AllowedCombination],
    timeout_sec: float = 30.0,
) -> list[dict[str, str]]:
    """LLM 抽取候选关系；失败/超时/JSON 解析失败均回退空列表，不阻塞摄取流程。

    relation_types/term_types/allowed_combinations 是该租户当前已确认
    （status="confirmed"）的本体 schema——调用方（见 graph_extraction.py）
    负责查出这三份列表再传进来，这个函数本身不碰数据库。抽取严格限定在
    这个范围内，不再是过去硬编码的 10 种通用关系类型，见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 E。

    ……（原有说明其余部分不变）
    """
    if not segments:
        return []

    system_prompt = _build_system_prompt(
        relation_types=relation_types, term_types=term_types,
        allowed_combinations=allowed_combinations,
    )
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": _build_user_content(segments)},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
```

- [ ] **Step 3: 解析结果新增 subject_type/object_type 字段**

`app/graphrag/llm_extractor.py` 第 117-134 行的解析循环：

```python
    relations: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        obj = str(item.get("object", "")).strip()
        relation_type = str(item.get("relation_type", "")).strip()
        subject_type = str(item.get("subject_type", "")).strip()
        object_type = str(item.get("object_type", "")).strip()
        evidence = str(item.get("evidence") or "").strip()
        if subject and obj and relation_type:
            relations.append(
                {
                    "subject": subject,
                    "object": obj,
                    "relation_type": relation_type,
                    "subject_type": subject_type,
                    "object_type": object_type,
                    "evidence": evidence,
                }
            )
    return relations
```

注意：这里不因为 `subject_type`/`object_type` 为空就丢弃整条候选——LLM 偶尔漏填类型字段时，下游 `normalize_and_write_relations`（Task 6）里的确认范围校验会因为空字符串匹配不到任何 `allowed_combinations` 而自然把它降级转人工审核，不需要在这一层额外判断。

- [ ] **Step 4: 更新测试**

`tests/graphrag/test_llm_extractor.py` 里所有调用 `extract_candidate_relations(...)` 的地方需要补上 `relation_types`/`term_types`/`allowed_combinations` 三个新的必填关键字参数。打开这个文件，对每一处调用按下面的模式补齐（用测试场景里实际涉及的类型/关系值，不是随便填）：

```python
    written = await extract_candidate_relations(
        segments,
        llm_registry=fake_registry,
        llm_provider_name="fake",
        relation_types=["RELATED_TO", "ADDRESSED_BY"],
        term_types=["error_code", "solution"],
        allowed_combinations=[
            AllowedCombination(subject_term_type="error_code", relation_type="ADDRESSED_BY", object_term_type="solution"),
        ],
    )
```

如果原测试断言了 system prompt 的具体文本内容（grep 这个文件里是否有类似 `assert "RELATED_TO" in captured_prompt` 或检查 `_SYSTEM_PROMPT` 常量本身的用例），把断言改成检查动态生成的 prompt 是否包含传入的 `relation_types`/`term_types` 值，而不是断言原来硬编码的 10 种类型文本。新增一个用例验证类型字段被正确解析：

```python
async def test_extract_candidate_relations_parses_type_fields(...):
    fake_registry = FakeRegistry(response_json={
        "relations": [
            {"subject": "错误码E509", "subject_type": "error_code",
             "object": "重启路由器", "object_type": "solution",
             "relation_type": "ADDRESSED_BY", "evidence": "……"},
        ]
    })
    relations = await extract_candidate_relations(
        ["文档片段"], llm_registry=fake_registry, llm_provider_name="fake",
        relation_types=["ADDRESSED_BY"], term_types=["error_code", "solution"],
        allowed_combinations=[
            AllowedCombination(subject_term_type="error_code", relation_type="ADDRESSED_BY", object_term_type="solution"),
        ],
    )
    assert relations == [{
        "subject": "错误码E509", "subject_type": "error_code",
        "object": "重启路由器", "object_type": "solution",
        "relation_type": "ADDRESSED_BY", "evidence": "……",
    }]
```

（`FakeRegistry`/等价的假 LLM 注册表——用本文件已有的假实现，不要新建。）

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/graphrag/test_llm_extractor.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/llm_extractor.py tests/graphrag/test_llm_extractor.py
git commit -m "feat(graphrag): constrain LLM extraction to tenant's confirmed ontology"
```

---

## Task 6: 归一化写入侧新增确认范围校验

**Files:**
- Modify: `app/graphrag/normalization.py`
- Test: `tests/graphrag/test_normalization.py`

**Interfaces:**
- Consumes：Task 4 的 `enqueue_for_review(..., subject_type_candidate=, object_type_candidate=)`。
- Produces：`normalize_and_write_relations(relations, *, terms, graph_client, source, tenant_id, now, confirmed_relation_types: set[str], allowed_combinations: set[tuple[str, str, str]], review_conn=None) -> int`——两个新参数**必填**，无默认值。

- [ ] **Step 1: 函数签名加两个必填参数，AUTO_MERGED 分支前插入校验**

`app/graphrag/normalization.py` 第 75-100 行：

```python
async def normalize_and_write_relations(
    relations: list[dict[str, str]],
    *,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    now: datetime,
    confirmed_relation_types: set[str],
    allowed_combinations: set[tuple[str, str, str]],
    review_conn: aiosqlite.Connection | None = None,
) -> int:
    """候选关系归一化对齐术语表后写入图谱，返回成功写入数。

    任一侧未能对齐标准术语、关系类型不合法、或关系类型/实体类型组合不在
    该租户已确认范围内的候选都不会自动入库。

    confirmed_relation_types/allowed_combinations 是调用方（见
    graph_extraction.py）预先查好的该租户 status="confirmed" 范围——
    AUTO_MERGED 直写路径（两侧实体都精确对齐时）过去会跳过这层校验直接
    写图谱，现在两侧对齐之后还要再过一遍这层检查：relation_type 必须在
    confirmed_relation_types 里，且 (subject_type, relation_type,
    object_type) 必须在 allowed_combinations 里，任一条件不满足就降级
    转人工审核（reason="not_in_confirmed_ontology"），不再直接写图谱。
    见 docs/superpowers/specs/2026-08-19-data-entry-unification-design.md
    决策 E.3。

    ……（原有 review_conn 说明段落不变）
    """
```

- [ ] **Step 2: 精确匹配分支（两侧都对齐）插入确认范围校验**

`app/graphrag/normalization.py` 第 156-201 行，在两侧都成功解析出 `subject_std`/`object_std`、原本直接进入 `try: ... merge_relation(...)` 的地方之前，插入一段新校验：

```python
        subject_type = relation.get("subject_type", "")
        object_type = relation.get("object_type", "")
        combo = (subject_type, relation["relation_type"], object_type)
        if relation["relation_type"] not in confirmed_relation_types or combo not in allowed_combinations:
            logger.info(
                "关系候选两侧已对齐术语表，但类型组合不在已确认本体范围内，转人工审核 "
                "subject=%s(%s) object=%s(%s) relation_type=%s",
                relation["subject"], subject_type, relation["object"], object_type,
                relation["relation_type"],
            )
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="not_in_confirmed_ontology",
                    source=source,
                    tenant_id=tenant_id,
                    suggested_subject_standard_name=subject_std,
                    suggested_object_standard_name=object_std,
                    evidence=relation.get("evidence", ""),
                    subject_type_candidate=subject_type or None,
                    object_type_candidate=object_type or None,
                )
            continue
        try:
            # merge_relation 现在按 {tenant_id, node_key} MERGE 端点节点……（原有注释不变）
            subject_node_key = next(
                t.node_key for t in terms if t.standard_name == subject_std
            )
```

（这段新校验插在原有 `try:` 语句块**之前**，`try:` 块本身内容不变——它下面已有的 `except ValueError`分支作为格式/保留名的兜底防线继续保留，两层校验并存，见 spec 决策 E.3 说明。）

- [ ] **Step 3: enqueue_for_review 的其它调用点补两个新参数**

`app/graphrag/normalization.py` 里另外两处 `enqueue_for_review(...)` 调用（第 124-136 行"模糊匹配转审核"、第 143-154 行"未能对齐术语表"）也传上候选类型（即使这两处场景下类型信息只是"附带记录"，不参与本次是否入队的判断）：

```python
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
                    evidence=relation.get("evidence", ""),
                    subject_type_candidate=relation.get("subject_type") or None,
                    object_type_candidate=relation.get("object_type") or None,
                )
```

（对第 143-154 行"未能对齐术语表"那处调用做同样的补充。）

- [ ] **Step 4: 更新既有测试**

`tests/graphrag/test_normalization.py` 里所有 `normalize_and_write_relations(...)` 调用都要补 `confirmed_relation_types`/`allowed_combinations` 两个必填参数。在文件顶部（`_TERMS` 常量附近）新增两个覆盖现有用例场景的常量：

```python
_CONFIRMED_RELATION_TYPES = {
    "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
    "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
}
_ALLOWED_COMBINATIONS = {
    ("error_code", rt, "module") for rt in _CONFIRMED_RELATION_TYPES
} | {
    ("module", rt, "error_code") for rt in _CONFIRMED_RELATION_TYPES
}
```

（这两个常量故意做成"覆盖 `_TERMS` 里出现的 error_code/module 两种类型、所有关系类型全放行"的宽松组合——本文件现有测试大多不是在测确认范围校验本身，用宽松集合让它们继续通过；Step 5 会新增专门测试收窄场景的用例。）

给 `_TERMS` 里的 `Term(...)` 构造加上 `term_type` 对应的语义（本文件已有 `term_type="error_code"`/`term_type="module"`，不需要改）。

对每一处既有调用：

```python
    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )
```

用这个模式统一替换本文件里每一处 `normalize_and_write_relations(` 调用（grep 这个文件确认改全）。

- [ ] **Step 5: 新增测试覆盖确认范围校验**

```python
async def test_downgrades_to_review_when_relation_type_not_confirmed():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "subject_type": "error_code",
         "object": "认证模块", "object_type": "module",
         "relation_type": "UNCONFIRMED_TYPE"},
    ]
    conn = await _open_review_db()  # 沿用本文件/review_queue 测试已有的建库方式

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations=_ALLOWED_COMBINATIONS, review_conn=conn,
    )

    assert written == 0
    assert graph_client.written == []
    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "not_in_confirmed_ontology"


async def test_downgrades_to_review_when_type_combination_not_allowed():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "subject_type": "module",  # 故意传反类型
         "object": "认证模块", "object_type": "error_code",
         "relation_type": "RELATED_TO"},
    ]
    conn = await _open_review_db()

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md", tenant_id="t1",
        now=_NOW, confirmed_relation_types=_CONFIRMED_RELATION_TYPES,
        allowed_combinations={("error_code", "RELATED_TO", "module")},  # 只允许一个方向
        review_conn=conn,
    )

    assert written == 0
    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["reason"] == "not_in_confirmed_ontology"
```

（`_open_review_db` 换成本文件顶部 import 的 `ensure_review_schema` 配合一个内存 `aiosqlite.connect(":memory:")` 的现有模式——照抄文件里其它需要 `review_conn` 的测试怎么建连接。）

- [ ] **Step 6: 运行测试**

Run: `python -m pytest tests/graphrag/test_normalization.py -v`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add app/graphrag/normalization.py tests/graphrag/test_normalization.py
git commit -m "feat(graphrag): validate relation/type combinations against confirmed ontology on write"
```

---

## Task 7: 抽取触发点接入 schema 确认门禁与确认列表获取

**Files:**
- Modify: `app/ingestion/graph_extraction.py`
- Modify: `app/ingestion/pipeline.py`
- Test: `tests/ingestion/test_graph_extraction.py`

**Interfaces:**
- Consumes：Task 5 的 `extract_candidate_relations(..., relation_types=, term_types=, allowed_combinations=)`；Task 6 的 `normalize_and_write_relations(..., confirmed_relation_types=, allowed_combinations=)`；`app.graphrag.ontology_lifecycle.is_ontology_confirmed`；`app.graphrag.ontology_relations.list_relation_types`；`app.graphrag.ontology_categories.list_term_types`；`app.graphrag.ontology_constraints.list_allowed_combinations`。
- Produces：`extract_and_write_graph_relations(..., relation_types: list[str], term_types: list[str], allowed_combinations: list[AllowedCombination])`——三个新增必填参数；`_maybe_extract_graph_relations` 在调用前查 `is_ontology_confirmed`，未确认则跳过并记日志。

- [ ] **Step 1: `extract_and_write_graph_relations` 加三个必填参数并透传**

`app/ingestion/graph_extraction.py` 第 41-109 行：

```python
async def extract_and_write_graph_relations(
    chunks: list[Chunk],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    now: datetime,
    relation_types: list[str],
    term_types: list[str],
    allowed_combinations: list[AllowedCombination],
    review_conn: aiosqlite.Connection | None = None,
    extract_timeout_sec: float = 30.0,
    batch_max_chars: int = 3000,
    max_concurrency: int = 8,
) -> int:
    """摄取时的图谱构建……（原有说明不变）

    relation_types/term_types/allowed_combinations 是该租户当前已确认
    （status="confirmed"）的本体 schema，调用方（pipeline.py::
    _maybe_extract_graph_relations）负责查好再传进来——这个函数本身不碰
    ontology 相关的数据库表，只负责把它们转发给 extract_candidate_relations
    （约束抽取范围）和 normalize_and_write_relations（约束写入范围）。见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 E。
    """
    await graph_client.delete_relations_by_source(source, tenant_id=tenant_id)
    batches = _batch_chunks_by_char_budget(chunks, max_chars=batch_max_chars)
    semaphore = asyncio.Semaphore(max_concurrency)
    confirmed_relation_types_set = set(relation_types)
    allowed_combinations_set = {
        (c.subject_term_type, c.relation_type, c.object_term_type) for c in allowed_combinations
    }

    async def _process_batch(batch: list[Chunk]) -> list[dict[str, str]]:
        async with semaphore:
            return await extract_candidate_relations(
                [chunk.text for chunk in batch],
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                relation_types=relation_types,
                term_types=term_types,
                allowed_combinations=allowed_combinations,
                timeout_sec=extract_timeout_sec,
            )

    all_relation_lists = await asyncio.gather(
        *(_process_batch(batch) for batch in batches)
    )

    total_written = 0
    for relations in all_relation_lists:
        total_written += await normalize_and_write_relations(
            relations,
            terms=terms,
            graph_client=graph_client,
            source=source,
            tenant_id=tenant_id,
            now=now,
            confirmed_relation_types=confirmed_relation_types_set,
            allowed_combinations=allowed_combinations_set,
            review_conn=review_conn,
        )
    return total_written
```

顶部 import 加 `from app.graphrag.ontology_constraints import AllowedCombination`。

- [ ] **Step 2: `pipeline.py::_maybe_extract_graph_relations` 接入确认门禁+确认列表获取**

`app/ingestion/pipeline.py` 第 60-94 行：

```python
async def _maybe_extract_graph_relations(
    chunks: list[Chunk],
    *,
    source: str,
    tenant_id: str,
    now: datetime,
    graph_llm_registry: ProviderRegistry | None,
    graph_llm_provider_name: str | None,
    graph_terms: list[Term] | None,
    graph_client: GraphWriteClientProtocol | None,
    graph_review_conn: aiosqlite.Connection | None,
) -> None:
    """图谱抽取为可选步骤，四项必需参数任一缺失则直接跳过，不影响向量化写入路径。

    graph_review_conn 独立于这四项之外是可选项：未能对齐术语表的候选
    关系会转入人工待审核队列而非直接丢弃（见 normalize_and_write_relations）。

    该租户本体 schema 未确认（is_ontology_confirmed 为 False）时同样跳过
    图谱抽取这一步——但仅在 graph_review_conn 可用时才能做这个判断（它
    同时是 ontology 相关表所在的连接）；graph_review_conn 为 None 时无法
    判断确认状态，保持跳过判断前的既有行为（不额外拦截）。见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 E.4。
    """
    if not (
        graph_llm_registry
        and graph_llm_provider_name
        and graph_terms
        and graph_client is not None
    ):
        return
    if graph_review_conn is not None and not await is_ontology_confirmed(graph_review_conn, tenant_id):
        logger.info(
            "租户 %r 本体 schema 尚未确认，跳过文档 %r 的知识图谱抽取", tenant_id, source
        )
        return
    relation_types = (
        [rt.value for rt in await list_relation_types(graph_review_conn, tenant_id, status="confirmed")]
        if graph_review_conn is not None else []
    )
    term_types = (
        [tt.value for tt in await list_term_types(graph_review_conn, tenant_id, status="confirmed")]
        if graph_review_conn is not None else []
    )
    allowed_combinations = (
        await list_allowed_combinations(graph_review_conn, tenant_id, status="confirmed")
        if graph_review_conn is not None else []
    )
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=graph_llm_registry,
        llm_provider_name=graph_llm_provider_name,
        terms=graph_terms,
        graph_client=graph_client,
        source=source,
        tenant_id=tenant_id,
        now=now,
        relation_types=relation_types,
        term_types=term_types,
        allowed_combinations=allowed_combinations,
        review_conn=graph_review_conn,
    )
```

顶部 import 加：

```python
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
```

需要一个模块级 `logger = logging.getLogger(__name__)`（如果这个文件还没有，加在 import 区之后；如果已经有，直接用）。

- [ ] **Step 3: 更新测试**

`tests/ingestion/test_graph_extraction.py` 里所有 `extract_and_write_graph_relations(...)` 调用补 `relation_types`/`term_types`/`allowed_combinations` 三个必填参数（用测试场景里 `FakeGraphClient`/mock 断言涉及的关系类型作为 `relation_types`，比如已有测试用 `"RELATED_TO"` 就传 `relation_types=["RELATED_TO"]`，`term_types`/`allowed_combinations` 按测试用到的 `_TERMS` 类型给一个宽松覆盖集合，模式与 Task 6 Step 4 相同）。

`tests/ingestion/test_pipeline.py`（如果存在，检查是否直接测试 `_maybe_extract_graph_relations`/`_ingest_chunks`）新增一个用例验证 schema 未确认时跳过抽取：

```python
async def test_ingest_skips_graph_extraction_when_ontology_unconfirmed(...):
    # 搭建一个 graph_review_conn，只建表不写入任何 confirmed 状态的
    # tenant_relation_types 行（is_ontology_confirmed 应返回 False）
    ...
    await _ingest_chunks(
        chunks, path, embedding_registry=..., embedding_provider_name=...,
        vector_store=..., tenant_id="t1",
        graph_llm_registry=fake_registry, graph_llm_provider_name="fake",
        graph_terms=[...], graph_client=fake_graph_client,
        graph_review_conn=review_conn,
    )
    # 断言 fake_graph_client 没有收到任何 delete_relations_by_source/merge_relation 调用
    assert fake_graph_client.deleted_sources == []
```

（用本文件已有的测试夹具/假实现搭建，不新建 helper。）

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/ingestion/test_graph_extraction.py tests/ingestion/test_pipeline.py -v`
Expected: 全部 PASS。

Run: `python -m pytest tests/ -x -q`
Expected: 全部 PASS（这一步改动了两个被多处依赖的函数签名，跑一次全量后端测试确认没有遗漏的调用点）。

- [ ] **Step 5: Commit**

```bash
git add app/ingestion/graph_extraction.py app/ingestion/pipeline.py tests/ingestion/test_graph_extraction.py
git commit -m "feat(ingestion): gate graph extraction on confirmed ontology, thread confirmed schema through"
```

---

## Task 8: StandardNameInput 新增"创建为新实体"入口

**Files:**
- Modify: `frontend/src/admin/StandardNameInput.tsx`

**Interfaces:**
- Consumes：无新增。
- Produces：新增可选 prop `onCreateNew?: (query: string) => void`——传入时，且当前查询无任何匹配建议时，下拉列表末尾渲染一个"+ 创建为新实体"按钮。

- [ ] **Step 1: 加 `onCreateNew` prop，下拉在无匹配时也要渲染**

`frontend/src/admin/StandardNameInput.tsx`：

```typescript
interface StandardNameInputProps {
  value: string
  onChange: (value: string) => void
  terms: GraphTerm[]
  placeholder: string
  ariaLabel: string
  onCreateNew?: (query: string) => void
}

const MAX_SUGGESTIONS = 8

export function StandardNameInput({
  value,
  onChange,
  terms,
  placeholder,
  ariaLabel,
  onCreateNew,
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
  const showCreateNew = Boolean(onCreateNew) && query.length > 0 && suggestions.length === 0

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
      {isOpen && (suggestions.length > 0 || showCreateNew) && (
        <ul className="absolute z-10 mt-1 w-full border-2 border-ink bg-paper shadow-brutal-sm">
          {suggestions.map((term) => {
            const matchedAlias = term.standard_name.includes(query)
              ? null
              : term.aliases.find((alias) => alias.includes(query))
            return (
              <li key={term.standard_name}>
                <button
                  type="button"
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
          {showCreateNew && (
            <li>
              <button
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  onCreateNew?.(query)
                  setIsOpen(false)
                }}
                className="block w-full cursor-pointer border-t-2 border-ink px-3 py-2 text-left text-sm font-bold text-ink hover:bg-card"
              >
                + 创建为新实体"{query}"
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
```

关键变化：`isOpen && suggestions.length > 0` 改成 `isOpen && (suggestions.length > 0 || showCreateNew)`——原来无匹配时整个下拉都不渲染，现在无匹配但有 `onCreateNew` 时要渲染出只含"创建为新实体"这一项的下拉。`onCreateNew` 不传时（比如术语库管理页别的地方如果还在用这个组件）行为与改动前完全一致。

- [ ] **Step 2: 手动验证（配合 Task 9 一起验证，本任务本身不含可独立验证的页面行为）**

本任务不需要单独的手动验证——`onCreateNew` 目前没有任何调用方在传，行为对现有页面（本任务改动前后）完全一致，只有 Task 9 接入之后才能在浏览器里看到实际效果。用类型检查确认没有破坏现有用法。

- [ ] **Step 3: 运行测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/admin/StandardNameInput.tsx
git commit -m "feat(frontend): add create-new-entity affordance to StandardNameInput"
```

---

## Task 9: 「非结构化数据加工」页接入内联创建实体

**Files:**
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`

**Interfaces:**
- Consumes：Task 2 的 `createTerm(sessionToken, tenantId, term: TermRecord)`（`termsApi.ts`，`term.source` 显式传 `'review'`）；Task 4 的 `subject_type_candidate`/`object_type_candidate`（`PendingReview` 接口新增两个字段）；Task 8 的 `StandardNameInput` 的 `onCreateNew` prop；`/api/admin/ontology/{tenantId}/term-types?status=confirmed`、`/api/admin/ontology/product-lines`（TermsPage.tsx 已有的同类调用，本任务照抄同样的 fetch 模式）。
- Produces：无新增导出，页面组件本身。

- [ ] **Step 1: `PendingReview` 接口加类型候选字段**

`frontend/src/admin/GraphReviewsPage.tsx` 第 11-21 行：

```typescript
interface PendingReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  source: string
  evidence: string
  suggested_subject_standard_name: string | null
  suggested_object_standard_name: string | null
  subject_type_candidate: string | null
  object_type_candidate: string | null
}
```

- [ ] **Step 2: 加载已确认的实体类型/产品线枚举（供内联创建表单下拉用）**

照抄 `TermsPage.tsx` 第 68-89 行的模式，在 `GraphReviewsPage` 组件内新增：

```typescript
  const [termTypeOptions, setTermTypeOptions] = useState<string[]>([])
  const [productLineOptions, setProductLineOptions] = useState<string[]>([])

  useEffect(() => {
    if (!sessionToken) return
    Promise.all([
      adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken)
        .then((res) => res.json())
        .then((data: { term_types: { value: string }[] }) =>
          setTermTypeOptions(data.term_types.map((t) => t.value)),
        )
        .catch((err) => {
          console.error('加载实体类型枚举失败', err)
          return null
        }),
      adminFetch('/api/admin/ontology/product-lines', sessionToken)
        .then((res) => res.json())
        .then((data: { product_lines: string[] }) => setProductLineOptions(data.product_lines))
        .catch((err) => {
          console.error('加载产品线枚举失败', err)
          return null
        }),
    ])
  }, [sessionToken, tenantId])
```

- [ ] **Step 3: 内联创建表单状态**

```typescript
  interface CreateEntityDraft {
    reviewId: number
    field: 'subject' | 'object'
    standardName: string
    termType: string
    productLine: string
    step: 'form' | 'confirm'
    submitting: boolean
    error: string | null
  }

  const [createDraft, setCreateDraft] = useState<CreateEntityDraft | null>(null)
  const [justCreated, setJustCreated] = useState<Record<number, { subject: boolean; object: boolean }>>({})
```

`justCreated` 记录哪些审核行的 subject/object 是本次会话里刚通过内联创建绑定的，驱动"新建"标签的显示（只在前端会话内生效，刷新页面后不再显示——这符合"视觉区分刚发生的操作"这个目的，不需要持久化）。

- [ ] **Step 4: 打开创建表单（`StandardNameInput` 的 `onCreateNew` 回调）**

```typescript
  const handleOpenCreateEntity = (reviewId: number, field: 'subject' | 'object', query: string) => {
    const review = pending.find((r) => r.review_id === reviewId)
    const suggestedType =
      (field === 'subject' ? review?.subject_type_candidate : review?.object_type_candidate) ?? ''
    setCreateDraft({
      reviewId,
      field,
      standardName: query,
      termType: suggestedType,
      productLine: '',
      step: 'form',
      submitting: false,
      error: null,
    })
  }
```

- [ ] **Step 5: 提交创建（含二次确认、撞名处理、成功后刷新+回填+标签）**

```typescript
  const handleSubmitCreateEntity = async () => {
    if (!sessionToken || !createDraft) return
    if (createDraft.step === 'form') {
      if (!createDraft.termType || !createDraft.productLine) return
      setCreateDraft({ ...createDraft, step: 'confirm', error: null })
      return
    }
    // step === 'confirm'：真正提交
    setCreateDraft({ ...createDraft, submitting: true, error: null })
    try {
      await createTerm(sessionToken, tenantId, {
        standard_name: createDraft.standardName,
        aliases: [],
        term_type: createDraft.termType,
        product_line: createDraft.productLine,
        source: 'review',
      })
      const { reviewId, field, standardName } = createDraft
      setDrafts((prev) => ({
        ...prev,
        [reviewId]: { ...prev[reviewId], [field]: standardName },
      }))
      setJustCreated((prev) => ({
        ...prev,
        [reviewId]: { ...prev[reviewId], [field]: true },
      }))
      setCreateDraft(null)
      // 创建成功后立即重新拉取本页 graphTerms，让同页其它引用同一新实体
      // 的候选行也能立刻搜到它——见 spec 决策 A.4。
      const refreshedTerms = await fetchGraphTerms(sessionToken, tenantId)
      setGraphTerms(refreshedTerms)
    } catch (err) {
      const message = err instanceof Error ? err.message : '创建实体失败'
      // 撞名冲突（TermNameConflictError 映射成 400）时，提示直接从下拉选择，
      // 并自动重新拉取一次 graphTerms——见 spec 决策 A.8。
      setCreateDraft({
        ...createDraft,
        step: 'confirm',
        submitting: false,
        error: `${message}，请刷新后从下拉列表中选择已有项`,
      })
      fetchGraphTerms(sessionToken, tenantId).then(setGraphTerms).catch(() => {})
    }
  }

  const handleCancelCreateEntity = () => setCreateDraft(null)
```

`createTerm` 的导入需要加进本文件顶部的 import：`import { fetchGraphTerms, createTerm, type GraphTerm } from './termsApi'`。

- [ ] **Step 6: `StandardNameInput` 接入 `onCreateNew`，展示"新建"标签**

`GraphReviewsPage.tsx` 第 446-477 行区域，两处 `StandardNameInput` 分别加 `onCreateNew`，并在旁边按 `justCreated` 状态渲染标签：

```tsx
            <div className="flex gap-3">
              <div className="flex flex-1 items-center gap-2">
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
                  onCreateNew={(query) => handleOpenCreateEntity(review.review_id, 'subject', query)}
                />
                {justCreated[review.review_id]?.subject && (
                  <span className="border border-status-success px-1.5 py-0.5 text-xs text-status-success">
                    新建
                  </span>
                )}
              </div>
              <div className="flex flex-1 items-center gap-2">
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
                  onCreateNew={(query) => handleOpenCreateEntity(review.review_id, 'object', query)}
                />
                {justCreated[review.review_id]?.object && (
                  <span className="border border-status-success px-1.5 py-0.5 text-xs text-status-success">
                    新建
                  </span>
                )}
              </div>
            </div>
```

（`border-status-success`/`text-status-success` 用本项目现有的状态色 token——如果项目里没有 `status-success` 这个 token，改用现成的 `accent-pink` 或其它已在别处使用过的强调色，保持视觉一致，不新增自定义颜色。）

- [ ] **Step 7: 创建表单/确认框 UI**

在页面根 `<div>` 内、`return` 语句的合适位置（比如紧跟在 `{error && ...}` 之后）新增一个条件渲染的浮层表单：

```tsx
      {createDraft && (
        <div className="fixed inset-0 z-20 flex items-center justify-center bg-ink/40 p-4">
          <div className="flex w-full max-w-md flex-col gap-3 border-2 border-ink bg-paper p-5 shadow-brutal">
            {createDraft.step === 'form' && (
              <>
                <p className="text-sm font-bold text-ink">创建为新实体</p>
                <p className="text-sm text-ink-soft">标准名：{createDraft.standardName}</p>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  实体类型
                  <select
                    value={createDraft.termType}
                    onChange={(event) =>
                      setCreateDraft({ ...createDraft, termType: event.target.value })
                    }
                    className="border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
                  >
                    <option value="">（请选择）</option>
                    {termTypeOptions.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  产品线
                  <select
                    value={createDraft.productLine}
                    onChange={(event) =>
                      setCreateDraft({ ...createDraft, productLine: event.target.value })
                    }
                    className="border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
                  >
                    <option value="">（请选择）</option>
                    {productLineOptions.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={handleSubmitCreateEntity}
                    disabled={!createDraft.termType || !createDraft.productLine}
                    className="min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    下一步
                  </button>
                  <button
                    type="button"
                    onClick={handleCancelCreateEntity}
                    className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm"
                  >
                    取消
                  </button>
                </div>
              </>
            )}
            {createDraft.step === 'confirm' && (
              <>
                <p className="text-sm font-bold text-ink">确认创建</p>
                <p className="text-sm text-ink">
                  标准名：{createDraft.standardName}
                  <br />
                  实体类型：{createDraft.termType}
                  <br />
                  产品线：{createDraft.productLine}
                </p>
                {createDraft.error && (
                  <p role="alert" className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink">
                    {createDraft.error}
                  </p>
                )}
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={handleSubmitCreateEntity}
                    disabled={createDraft.submitting}
                    className="min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {createDraft.submitting ? '创建中…' : '确认创建'}
                  </button>
                  <button
                    type="button"
                    onClick={handleCancelCreateEntity}
                    disabled={createDraft.submitting}
                    className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    取消
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
```

- [ ] **Step 8: 手动验证**

启动前后端开发服务器，在一个本体 schema 已确认、有未匹配候选实体名的租户下：
1. 打开「非结构化数据加工」页待审核列表，在 subject/object 输入框输入一个术语表里完全不存在的名字，确认下拉列表末尾出现"+ 创建为新实体"。
2. 点击后确认弹出表单，`term_type` 下拉默认选中了该候选行的 `subject_type_candidate`/`object_type_candidate`（如果有）。
3. 不选类型/产品线时"下一步"按钮禁用；选好后点"下一步"进入二次确认框，展示即将创建的三个字段。
4. 确认后创建成功：该候选行的 subject/object 输入框自动填入新标准名，旁边出现"新建"标签；如果同页还有其它候选行引用了同一个名字，它们的下拉列表能立即搜到这个新实体。
5. 故意用一个已存在的标准名走一遍内联创建流程，确认二次确认框报错并提示"请刷新后从下拉列表中选择已有项"。

- [ ] **Step 9: 运行测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 10: Commit**

```bash
git add frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(frontend): inline entity creation on unstructured data processing page"
```

---

## Task 10: 侧边栏与路由重组为「数据填充」+ 三个可深链接子 tab

**Files:**
- Create: `frontend/src/admin/DataEntryPage.tsx`
- Modify: `frontend/src/admin/AdminLayout.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes：Task 3 的 `TermsPage`（重命名标题已在 Task 3 完成）、`SchemaEtlPage`、`GraphReviewsPage`（Task 9 完成后的版本）。
- Produces：新路由 `/admin/data-entry/manual`、`/admin/data-entry/etl`、`/admin/data-entry/review`；旧路由 `/admin/terms`、`/admin/graph-reviews`、`/admin/schema-etl` 重定向到对应新路径。

- [ ] **Step 1: 新建 `DataEntryPage.tsx` 容器组件**

`frontend/src/admin/DataEntryPage.tsx`：

```typescript
import { NavLink, Outlet } from 'react-router-dom'

const subTabClass = ({ isActive }: { isActive: boolean }) =>
  `min-h-[44px] cursor-pointer border-2 border-ink px-4 py-2 text-sm font-bold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink ${
    isActive ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink'
  }`

export function DataEntryPage() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">数据填充</h1>
      <div className="flex gap-2">
        <NavLink to="/admin/data-entry/manual" className={subTabClass}>
          实体列表
        </NavLink>
        <NavLink to="/admin/data-entry/etl" className={subTabClass}>
          结构化数据加工
        </NavLink>
        <NavLink to="/admin/data-entry/review" className={subTabClass}>
          非结构化数据加工
        </NavLink>
      </div>
      <Outlet />
    </div>
  )
}
```

`TermsPage`/`SchemaEtlPage`/`GraphReviewsPage` 各自页面顶部原来的 `<h1>` 标题（分别是"实体列表"、"ETL 跑批（租户：...）"、"知识图谱审核（租户：...）"）继续保留在各自组件里不用删——`DataEntryPage` 的横向子 tab 是导航，子页面内部的 `<h1>` 是该子页面自己的上下文标题（比如显示租户名），两者不冲突，只是视觉上会有两层标题，这是可接受的（与 `OntologySchemaPage.tsx` 页面标题+内部 tab 标题共存的现状一致）。

- [ ] **Step 2: `App.tsx` 路由重组**

`frontend/src/App.tsx` 第 14-22 行区域：

```tsx
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<Navigate to="ontology" replace />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="data-entry" element={<DataEntryPage />}>
          <Route index element={<Navigate to="manual" replace />} />
          <Route path="manual" element={<TermsPage />} />
          <Route path="etl" element={<SchemaEtlPage />} />
          <Route path="review" element={<GraphReviewsPage />} />
        </Route>
        <Route path="graph-reviews" element={<Navigate to="/admin/data-entry/review" replace />} />
        <Route path="terms" element={<Navigate to="/admin/data-entry/manual" replace />} />
        <Route path="schema-etl" element={<Navigate to="/admin/data-entry/etl" replace />} />
        <Route path="ontology" element={<OntologySchemaPage />} />
      </Route>
```

（如果原文件第 16-22 行 `<Route path="/admin" ...>` 下已经有一个 `index` 路由，保留原有的，不要重复添加——本步骤只新增/替换 `documents` 之后到 `ontology` 之前的这几行,具体以实际文件内容为准，不要破坏其它未提及的路由。）

顶部 import 区加：

```tsx
import { DataEntryPage } from './admin/DataEntryPage'
```

删除不再需要的直接 import（如果 `TermsPage`/`SchemaEtlPage`/`GraphReviewsPage` 原本是直接 import 到 `App.tsx` 用于旧路由的，改成被 `DataEntryPage` 内部路由使用后仍然需要在 `App.tsx` 里 import，因为这几个 `<Route path="manual" element={<TermsPage />} />` 还在 `App.tsx` 里声明——不需要删除这几个 import，只是使用它们的 `<Route>` 挂载点变了）。

- [ ] **Step 3: `AdminLayout.tsx` 侧边栏合并为一项**

`frontend/src/admin/AdminLayout.tsx` 第 31-47 行：

```tsx
          <nav className="flex flex-row flex-wrap gap-2 md:flex-col">
            <NavLink to="/admin/ontology" className={navLinkClass}>
              本体 Schema 管理
            </NavLink>
            <NavLink to="/admin/documents" className={navLinkClass}>
              文档管理
            </NavLink>
            <NavLink to="/admin/data-entry" className={navLinkClass}>
              数据填充
            </NavLink>
          </nav>
```

（原来的「知识图谱审核」「ETL 跑批」「术语库管理」三个 `<NavLink>` 删除，替换成这一个「数据填充」。）

- [ ] **Step 4: 手动验证**

启动前端开发服务器：
1. 侧边栏只剩「本体 Schema 管理」「文档管理」「数据填充」三项（外加登出/返回前台等非导航按钮）。
2. 点击「数据填充」默认落到"实体列表"子 tab（URL 是 `/admin/data-entry/manual`）。
3. 点击「结构化数据加工」子 tab，URL 变成 `/admin/data-entry/etl`，页面内容是原 ETL 跑批页；直接刷新浏览器，页面仍停留在这个子 tab（验证可深链接）。
4. 同样验证「非结构化数据加工」子 tab（`/admin/data-entry/review`）。
5. 直接在地址栏访问旧路径 `/admin/terms`、`/admin/graph-reviews`、`/admin/schema-etl`，确认都被重定向到对应的新路径，不是 404。

- [ ] **Step 5: 运行测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/admin/DataEntryPage.tsx frontend/src/admin/AdminLayout.tsx frontend/src/App.tsx
git commit -m "feat(frontend): merge data-entry pages under one sidebar entry with deep-linkable sub-tabs"
```

---

## 最终验证（写给执行本计划的 controller，不是单独一个 task）

全部 10 个 Task 完成后，在进入 `superpowers:subagent-driven-development` 的最终整体 review 之前，controller 自己应确认：

- `python -m pytest tests/ -q` 全量通过。
- `cd frontend && npx tsc --noEmit` 无类型错误。
- 对照 spec 文档的"验收标准"一节逐条核对：内联创建+同页刷新、`terms.source` 全链路、实体列表无创建入口、导航合并+深链接+旧路由重定向、未确认 schema 跳过抽取、AUTO_MERGED 路径的确认范围校验。
