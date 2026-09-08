# 导航重排与看板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后台从三个流程组 + 一个孤儿组变成六个模块，并新增一个跨领域的看板作为登录落地页。

**Architecture:** 导航结构的单一事实来源是 `frontend/src/adminRoutes.ts`（它已有一套断言「每个目的地都出现在侧边栏」的测试）。看板是唯一的组织级页面，不需要「当前租户」，数字全部实时算（spec D5）——边计数从节点侧发起走已有索引，每个领域一张卡各自独立请求各自落位。

**Tech Stack:** FastAPI · aiosqlite · Neo4j · React 18 + TypeScript + Vite · vitest

**Spec:** `docs/superpowers/specs/2026-09-08-interaction-redesign-design.md`

**Depends on:** `docs/superpowers/plans/2026-09-08-multi-persona-foundation.md`（看板按组织聚合，且要用 `deps.list_accessible_tenant_ids`）

## Global Constraints

同 `2026-09-08-multi-persona-foundation.md` 的十二条，逐字适用。额外两条：

13. **看板只显示这个账号有权访问的领域。** 聚合数字也是信息——给 member 看到他无权访问的领域有多少实体，等于泄露业务规模。
14. **垫片一跳直达，不叠成链。** `LEGACY_REDIRECTS` 已有两代，那份文件的注释专门警告过链式跳转。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `frontend/src/adminRoutes.ts`（改） | 六模块的路由表与导航结构，单一事实来源 |
| `frontend/src/App.tsx`（改） | 新路径 + 垫片 |
| `app/api/admin_dashboard_routes.py`（新） | 看板的两个端点：领域清单、单领域统计 |
| `app/graphrag/tenant_stats.py`（新） | 单个租户的统计：实体 / 关系 / 文档 / 待办 |
| `app/graphrag/neo4j_client.py`（改） | `count_relation_edges_for_tenant`（节点侧发起） |
| `frontend/src/admin/DashboardPage.tsx`（新） | 看板页 |
| `frontend/src/admin/DomainCard.tsx`（新） | 一个领域一张卡，自己负责取数与骨架屏 |

---

## Task 1: 图谱侧的租户关系计数

**Files:**
- Modify: `app/graphrag/neo4j_client.py`（在 `_SUMMARIZE_TERM_RELATION_EDGES_QUERY` 附近加一条查询与一个方法）
- Modify: `app/graphrag/neptune_client.py`（NotImplementedError 桩）
- Test: `tests/graphrag/test_neo4j_client.py`（追加）

**Interfaces:**
- Produces: `async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int`（同时加进 `GraphWriteProtocol`）

- [ ] **Step 1: 写失败测试**

在 `tests/graphrag/test_neo4j_client.py` 追加：

```python
async def test_count_relation_edges_for_tenant_starts_from_the_indexed_nodes():
    """从节点侧发起，不是全库扫关系。

    Term(tenant_id, node_key) 上有索引（term_tenant_node_key_idx），
    从 (t:Term {tenant_id: $tenant_id}) 起手能走它；反过来以 ()-[r]-() 起手
    则是全库扫描——demo 那张图百万级边，而看板是登录后的第一屏。
    """
    session = FakeSession(rows=[{"edge_count": 1204883}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_tenant(tenant_id="demo")

    assert count == 1204883
    assert session.last_parameters == {"tenant_id": "demo"}
    assert "MATCH (t:Term {tenant_id: $tenant_id})" in session.last_query


async def test_count_relation_edges_for_tenant_counts_outgoing_only():
    """只数出边。无向匹配会让每条边被两端各数一次，看板上的关系数直接翻倍——
    而用户拿它跟实体详情页里数出来的边核对时会发现对不上。"""
    session = FakeSession(rows=[{"edge_count": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.count_relation_edges_for_tenant(tenant_id="demo")

    assert "-[r]->()" in session.last_query
    assert "-[r]-()" not in session.last_query.replace("-[r]->()", "")


async def test_count_relation_edges_for_tenant_filters_edge_tenant_and_skips_alias():
    """口径跟详情页一致：r.tenant_id 过滤 + 排除 ALIAS_OF。

    别名边是词表→图谱的结构性同步边，不是知识图谱数据。算进去的话，
    看板上的关系数会比用户在任何别的地方看到的都大一截。
    """
    session = FakeSession(rows=[{"edge_count": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.count_relation_edges_for_tenant(tenant_id="demo")

    assert "r.tenant_id = $tenant_id" in session.last_query
    assert "type(r) <> 'ALIAS_OF'" in session.last_query


async def test_count_relation_edges_for_tenant_returns_zero_when_no_rows():
    """空图时 Cypher 仍会给出一行、count 为 0；但防御性地处理无行的情况——
    返回 None 会让看板显示「null 条关系」。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    assert await client.count_relation_edges_for_tenant(tenant_id="demo") == 0
```

- [ ] **Step 2: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_neo4j_client.py -q -p no:cacheprovider`

- [ ] **Step 3: 写实现**

在 `app/graphrag/neo4j_client.py` 中 `_SUMMARIZE_TERM_RELATION_EDGES_QUERY` 之前插入：

```python
_COUNT_TENANT_RELATION_EDGES_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id})-[r]->()
WHERE r.tenant_id = $tenant_id AND type(r) <> 'ALIAS_OF'
RETURN count(r) AS edge_count
"""
# 看板上「这个领域有多少关系」。
#
# 从节点侧发起而不是 ()-[r]-()：Term(tenant_id, node_key) 上有索引
# （term_tenant_node_key_idx），从 (t:Term {tenant_id: $tenant_id}) 起手能走它；
# 反过来以关系起手是全库扫描，而关系属性上没有索引。demo 那张图百万级边，
# 看板又是登录后的第一屏——这个差别是「秒开」和「以为它坏了」的差别。
#
# 只数出边（-[r]->()）：无向匹配会让每条边被两端各数一次，看板上的数字
# 直接翻倍，而用户拿它跟实体详情页数出来的边核对时会发现对不上。
#
# 过滤口径跟 _TERM_RELATIONS_QUERY / _SUMMARIZE_TERM_RELATION_EDGES_QUERY
# 一致：r.tenant_id 过滤 + 排除 ALIAS_OF。别名边是词表→图谱的结构性同步边，
# 不是知识图谱数据。
```

方法实现（放在 `summarize_relation_edges_for_terms` 附近）：

```python
    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int:
        """这个租户图里有多少条关系边。看板用。"""
        async with self._driver.session() as session:
            result = await session.run(
                _COUNT_TENANT_RELATION_EDGES_QUERY, {"tenant_id": tenant_id}
            )
            rows = await result.data()
            return rows[0]["edge_count"] if rows else 0
```

`GraphWriteProtocol` 里加：

```python
    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int: ...
```

`app/graphrag/neptune_client.py` 加桩，照抄邻近方法的写法。

- [ ] **Step 4: 跑测试确认它绿 + 变异**

```bash
# 变异 A：MATCH 改成 ()-[r]->() （不从节点侧起手）
#   预期红：test_..._starts_from_the_indexed_nodes
# 变异 B：-[r]->() 改成 -[r]-()
#   预期红：test_..._counts_outgoing_only
# 变异 C：去掉 type(r) <> 'ALIAS_OF'
#   预期红：test_..._filters_edge_tenant_and_skips_alias
```

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/neo4j_client.py app/graphrag/neptune_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(graph): 按租户数关系边，从节点侧发起走已有索引"
```

---

## Task 2: 单领域统计

**Files:**
- Create: `app/graphrag/tenant_stats.py`
- Test: `tests/graphrag/test_tenant_stats.py`

**Interfaces:**
- Consumes:
  - `app.graphrag.terms_store.count_terms_merged(conn, tenant_id, *, source=None) -> int`
  - Task 1 的 `count_relation_edges_for_tenant(*, tenant_id) -> int`
  - `app.graphrag.review_queue.count_pending_reviews(conn, *, tenant_id) -> int`
- Produces:
  - `@dataclass(frozen=True) class TenantStats`：`tenant_id: str` · `term_count: int` · `edge_count: int` · `document_count: int` · `pending_review_count: int`
  - `async def collect_tenant_stats(review_conn, ingestion_conn, graph_client, *, tenant_id: str) -> TenantStats`

- [ ] **Step 1: 写失败测试**

创建 `tests/graphrag/test_tenant_stats.py`。四个数字必须**互不相同**——相同的话，
把它们接错线的实现也能变绿：

```python
def test_collects_all_four_numbers_from_the_right_sources():
    """四个数字互不相同：13 / 27 / 5 / 3。相同的话，把实体数接到关系数
    那一格的实现也能变绿。"""


def test_each_number_is_scoped_to_this_tenant():
    """另一个租户的数据必须不出现在这个租户的统计里。seed 两个租户，
    断言各自拿到自己那份。"""


def test_a_graph_failure_surfaces_instead_of_reporting_zero_edges():
    """图谱查不通时抛出去，不是报 0 条关系。

    报 0 的话，用户看到的是「这个领域一条关系都没有」——一个看起来正常、
    实际是错的数字。他会据此以为数据没导进去，然后去重跑一遍 ETL。
    """
    with pytest.raises(RuntimeError):
        ...
```

补全成可运行用例（`FakeGraph` 按 Task 1 的方法签名实现）。

- [ ] **Step 2: 确认文档计数怎么来**

Run: `grep -n "ingested_documents" -A12 app/ingestion/*.py app/graphrag/*.py | head -30`

`ingested_documents` 表在哪个连接下、有没有现成的计数函数，先查清楚。
没有现成的就在 `tenant_stats.py` 里直接写 `SELECT COUNT(*) FROM ingested_documents
WHERE tenant_id = ?`——**并且带上 tenant_id 条件**（Global Constraint 8）。

- [ ] **Step 3: 跑测试确认它红 → 写实现 → 绿**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import aiosqlite

from app.graphrag.review_queue import count_pending_reviews
from app.graphrag.terms_store import count_terms_merged


@dataclass(frozen=True)
class TenantStats:
    """一个领域在看板上的四个数字。"""

    tenant_id: str
    term_count: int
    edge_count: int
    document_count: int
    pending_review_count: int


class EdgeCounter(Protocol):
    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int: ...


async def collect_tenant_stats(
    review_conn: aiosqlite.Connection,
    ingestion_conn: aiosqlite.Connection,
    graph_client: EdgeCounter,
    *,
    tenant_id: str,
) -> TenantStats:
    """一个领域的统计。全部实时算（spec D5）。

    图谱查不通时**抛出去**，不是报 0 条关系。报 0 的话用户看到的是「这个
    领域一条关系都没有」——一个看起来正常、实际是错的数字，他会据此以为
    数据没导进去然后去重跑一遍 ETL。让它抛出来，调用方把这张卡渲染成
    「统计失败」，用户看得见也够得着纠正。

    实体数走 count_terms_merged（合并视图）而不是 count_terms（裸表）：
    看板上的数字必须等于用户在实体明细页数得出来的条数。
    """
    term_count = await count_terms_merged(review_conn, tenant_id)
    edge_count = await graph_client.count_relation_edges_for_tenant(tenant_id=tenant_id)
    cursor = await ingestion_conn.execute(
        "SELECT COUNT(*) AS n FROM ingested_documents WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    document_count = row["n"] if row is not None else 0
    pending_review_count = await count_pending_reviews(review_conn, tenant_id=tenant_id)
    return TenantStats(
        tenant_id=tenant_id,
        term_count=term_count,
        edge_count=edge_count,
        document_count=document_count,
        pending_review_count=pending_review_count,
    )
```

- [ ] **Step 4: 变异 + 提交**

```bash
# 变异 A：图谱异常吞掉、edge_count = 0
#   预期红：test_a_graph_failure_surfaces_instead_of_reporting_zero_edges
# 变异 B：count_terms_merged 换成 count_terms（裸表）
#   预期红：需要一条用例覆盖——本任务补：造一条 __deleted__ 编辑，
#          断言统计里的实体数是合并视图的数，不是裸表的数
# 变异 C：文档计数去掉 WHERE tenant_id = ?
#   预期红：test_each_number_is_scoped_to_this_tenant
```

变异 B 若无用例覆盖，**必须补**——这正是看板数字和实体明细页对不上的那个 bug。

```bash
git add app/graphrag/tenant_stats.py tests/graphrag/test_tenant_stats.py
git commit -m "feat(dashboard): 单领域的四个统计数字，图谱查不通就抛不报零"
```

---

## Task 3: 看板 API

**Files:**
- Create: `app/api/admin_dashboard_routes.py`
- Modify: `app/main.py`（挂载）
- Test: `tests/api/test_admin_dashboard_routes.py`

**Interfaces:**
- Consumes: `deps.list_accessible_tenant_ids`、Task 2 的 `collect_tenant_stats`、`organizations_store.list_organizations`
- Produces:
  - `GET /api/admin/dashboard/domains` → `{"domains": [{tenant_id, name, org_id, org_name}]}` ——**不含统计数字**
  - `GET /api/admin/dashboard/domains/{tenant_id}/stats` → `{tenant_id, term_count, edge_count, document_count, pending_review_count}`

**为什么拆成两个端点**：spec D5 的裁决二要求「每个领域一张卡、各自独立请求、各自落位」。
一个端点返回全部统计的话，前端只能等最慢的那个领域算完才能画第一张卡。

- [ ] **Step 1: 写失败测试**

```python
def test_domains_endpoint_lists_only_what_this_account_can_reach(dashboard_conn):
    """看板只显示有权访问的领域。聚合数字也是信息——给 member 看到他无权
    访问的领域有多少实体，等于泄露业务规模（spec 裁决补充）。"""


def test_domains_endpoint_returns_no_numbers(dashboard_conn):
    """清单端点不带统计。带上的话它就得等所有领域算完，
    「各自落位」这个设计就没了。"""
    body = _get_domains(dashboard_conn).json()
    assert "term_count" not in body["domains"][0]


def test_stats_endpoint_refuses_a_tenant_this_account_cannot_reach(dashboard_conn):
    """逐领域端点必须走 require_tenant_access。清单端点过滤了、
    这个不校验的话，member 直接改 URL 就能拿到别人的统计。"""
    assert _get_stats(dashboard_conn, tenant_id="secret", role="member").status_code == 403


def test_stats_endpoint_returns_all_four_numbers(dashboard_conn):
    """四个数字互不相同，逐个断言。"""


def test_a_graph_failure_returns_an_error_not_zeroes(dashboard_conn):
    """图谱挂了时这张卡返回 5xx，前端据此渲染「统计失败」。
    返回全 0 的话用户会以为这个领域是空的。"""
    assert _get_stats(dashboard_conn, graph_broken=True).status_code >= 500


def test_domains_carry_their_organization(dashboard_conn):
    """卡片要按组织分组，所以清单里得有 org_id 和 org_name。
    没挂组织的租户 org_id 为 null——那是合法状态，不是错误。"""
```

补全成可运行用例。

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

实现要点：清单端点挂 `require_admin_session`（所有角色可调，内容按 accessible 过滤）；
统计端点挂 `require_tenant_access`。

- [ ] **Step 5: 变异**

```bash
# 变异 A：清单端点去掉 accessible 过滤
#   预期红：test_domains_endpoint_lists_only_what_this_account_can_reach
# 变异 B：统计端点的 require_tenant_access 换成 require_admin_session
#   预期红：test_stats_endpoint_refuses_a_tenant_this_account_cannot_reach
# 变异 C：统计端点 try/except 吞掉图谱异常返回 0
#   预期红：test_a_graph_failure_returns_an_error_not_zeroes
```

- [ ] **Step 6: 重启后端 + 提交**

```bash
git add app/api/admin_dashboard_routes.py tests/api/test_admin_dashboard_routes.py app/main.py
git commit -m "feat(api): 看板拆成清单与逐领域统计两个端点，卡片各自落位"
```

---

## Task 4: 导航重排

**Files:**
- Modify: `frontend/src/adminRoutes.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/admin/AdminLayout.tsx`（去掉 `NAV_STANDALONE` 那一段）
- Test: `frontend/src/adminRoutes.test.ts`（既有，需改）
- Test: `frontend/src/App.routing.test.tsx`（既有，需补垫片用例）

**Interfaces:**
- Produces: 六个 `NavGroup`，`NAV_STANDALONE` 与 `NAV_STANDALONE_LABEL` 删除

新的 `NAV_GROUPS`：

```ts
export const NAV_GROUPS: NavGroup[] = [
  { id: 'dashboard', label: '看板', items: [
    { path: ADMIN_ROUTES.dashboard, label: '看板', icon: LayoutDashboard },
  ] },
  { id: 'ontology', label: '本体创建', items: [
    { path: ADMIN_ROUTES.ontology, label: '本体结构', icon: Network },
    { path: ADMIN_ROUTES.ontologyGraph, label: '本体图', icon: Waypoints },
    { path: ADMIN_ROUTES.guidedOntology, label: '引导建模', icon: Wand2 },
    { path: ADMIN_ROUTES.persona, label: '数字人', icon: UserRound },
  ] },
  { id: 'import', label: '数据导入', items: [
    { path: ADMIN_ROUTES.documents, label: '文档导入', icon: FileText },
    { path: ADMIN_ROUTES.etl, label: '表格导入', icon: Table2 },
    { path: ADMIN_ROUTES.dbImport, label: '数据库导入', icon: Database },
  ] },
  { id: 'review', label: '数据审核', items: [
    { path: ADMIN_ROUTES.reviewRelations, label: '关系审核', icon: GitPullRequestArrow },
    { path: ADMIN_ROUTES.reviewConflicts, label: '属性冲突', icon: Scale },
    { path: ADMIN_ROUTES.reviewDuplicates, label: '疑似重复', icon: ScanSearch },
    { path: ADMIN_ROUTES.reviewDirtyEdges, label: '脏边与孤儿', icon: Unlink },
  ] },
  { id: 'browse', label: '结果预览', items: [
    { path: ADMIN_ROUTES.terms, label: '实体明细', icon: Boxes },
    { path: ADMIN_ROUTES.dataGraph, label: '图谱预览', icon: Share2 },
  ] },
  { id: 'logs', label: '日志明细', items: [
    { path: ADMIN_ROUTES.diagnostics, label: '问答明细', icon: Stethoscope },
    { path: ADMIN_ROUTES.errors, label: '报错明细', icon: TriangleAlert },
  ] },
]
```

**注意**：`dbImport` / `reviewConflicts` / `reviewDirtyEdges` / `dataGraph` / `errors`
这五个页面在阶段四~六才实现。本任务**先建路由和占位页**——占位页写明
「这个功能在阶段 N 交付」并链到对应的计划文件，**不是**空白页。
侧边栏里挂一个点进去空白的入口，比不挂更糟。

- [ ] **Step 1: 改既有的导航测试**

`frontend/src/adminRoutes.test.ts` 里有断言「七个目的地全部出现在侧边栏」和
`NOT_IN_NAV` 清单。按新结构改：

```ts
it('每一个租户内的目的地都出现在侧边栏里', () => {
  // 这条测试的存在理由：此前加页面要改四个地方（App.tsx、AdminLayout、
  // ⌘K 命令表、空状态链接），漏一个就是「页面存在但没人找得到」。
})

it('每个路由要么在导航里，要么在 NOT_IN_NAV 清单里', () => {
  // 新加页面忘了归类时这条会红，而不是默默走进错误的分支。
})

it('分组 id 与路径里的阶段段名一致', () => {
  // 既有断言，保留。分组和路径脱节的话，groupIdForPath 会返回 null，
  // 侧边栏默认展开哪一组就失灵了。
})
```

- [ ] **Step 2: 补垫片测试**

在 `frontend/src/App.routing.test.tsx` 追加：

```tsx
it.each([
  ['/admin/model/ontology', '/admin/ontology/ontology'],
  ['/admin/ingest/documents', '/admin/import/documents'],
  ['/admin/terms', '/admin/browse/terms'],
  ['/admin/diagnostics', '/admin/logs/qa'],
])('旧路径 %s 一跳直达 %s', (from, to) => {
  // 一跳直达，不叠成链：链式的问题不只是多一跳——「这条垫片指向哪」
  // 要顺着链子读，而且中间那一代哪天删掉就会断。
})

it('LEGACY_REDIRECTS 里没有指向自己的条目', () => {
  // /admin/browse/terms 曾经是第三代用过又废掉的路径，这次它回来了。
  // 那条旧垫片（'/admin/browse/terms' → terms）必须删掉，
  // 否则就是自己指向自己 = 无限重定向。
  Object.entries(LEGACY_REDIRECTS).forEach(([from, to]) => {
    expect(from).not.toBe(to)
  })
})

it('每条垫片的目标都是一个真实存在的路由', () => {
  // 指向一个不存在路径的垫片，用户点旧书签会落到 404，
  // 而他以为是自己的书签坏了。
})
```

- [ ] **Step 3: 跑测试确认它红 → 改 `adminRoutes.ts` → 绿**

`ADMIN_ROUTES` 新增：`dashboard: '/admin/dashboard'`、`persona: '/admin/ontology/persona'`、
`dbImport: '/admin/import/database'`、`reviewConflicts: '/admin/review/conflicts'`、
`reviewDirtyEdges: '/admin/review/dirty-edges'`、`dataGraph: '/admin/browse/graph'`、
`errors: '/admin/logs/errors'`。

既有的 `ontology` / `ontologyGraph` / `guidedOntology` / `documents` / `etl` /
`terms` / `diagnostics` 改成新路径。

`LEGACY_REDIRECTS` 补上全部旧→新，并**删掉** `'/admin/browse/terms'` 那一条。

`NAV_STANDALONE` / `NAV_STANDALONE_LABEL` 删除，`AdminLayout.tsx` 里渲染它们的那一段
（分隔线 + `<p>` 标题 + `NAV_STANDALONE.map`）一并删掉。

`TENANT_SCOPED_ROUTE_KEYS` 补上新增的六个；**`dashboard` 归入 `NON_TENANT_ROUTE_KEYS`**
——它是组织级页面，不需要「当前租户」。

- [ ] **Step 4: `/admin` 落地页改成看板**

找到 `App.tsx` 里 `/admin` 的 index route（当前应该重定向到某个页面），改成
重定向到 `ADMIN_ROUTES.dashboard`。

- [ ] **Step 5: 建五个占位页**

每个占位页的内容形如：

```tsx
export function DatabaseImportPage() {
  return (
    <EmptyState
      icon={Database}
      title="数据库导入还没上线"
      action="它在实施计划的阶段六（docs/superpowers/plans/2026-09-08-database-import.md）。
              在那之前，数据库里的数据可以先导出成 CSV 走「表格导入」。"
    />
  )
}
```

**必须给出一条现在就能走的替代路径**。只说「未上线」的话，用户点进来一无所获。

- [ ] **Step 6: 跑前端全量 + 类型检查**

```
cd frontend && npx vitest run src
cd frontend && npx tsc --noEmit
```

既有用例里凡是硬编码了旧路径的都会红——**逐条改成新路径**，
不要为了让它们绿而在 `ADMIN_ROUTES` 里保留旧值。

- [ ] **Step 7: 变异**

```bash
# 变异 A：删掉 NAV_GROUPS 里的一项（比如「数字人」）
#   预期红：test「每一个租户内的目的地都出现在侧边栏里」
# 变异 B：LEGACY_REDIRECTS 里加一条 '/admin/browse/terms' → '/admin/browse/terms'
#   预期红：test「没有指向自己的条目」
# 变异 C：垫片改成两跳（旧 → 中间代 → 新）
#   预期红：test.each「一跳直达」
# 变异 D：dashboard 从 NON_TENANT_ROUTE_KEYS 挪进 TENANT_SCOPED
#   预期红：需要一条用例覆盖——本任务补：currentTenantId 为 null 时
#          看板照常渲染，不被「请先选择一个租户」空态挡住
```

变异 D 若无用例覆盖，**必须补**——没选租户时看板被挡住的话，
新登录的 admin 会看到一屏空态而不是他的看板。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/adminRoutes.ts frontend/src/adminRoutes.test.ts frontend/src/App.tsx frontend/src/App.routing.test.tsx frontend/src/admin/AdminLayout.tsx
git commit -m "refactor(admin): 后台改成六模块，孤儿组「明细查询」并进结果预览与日志明细"
```

占位页文件单独一次提交。

---

## Task 5: 看板页

**Files:**
- Create: `frontend/src/admin/DashboardPage.tsx`
- Create: `frontend/src/admin/DomainCard.tsx`
- Test: `frontend/src/admin/dashboard.test.tsx`

**Interfaces:**
- Consumes: Task 3 的两个端点
- Produces: `DashboardPage`（按组织分组渲染 `DomainCard`）、`DomainCard`（自己取数、自己出骨架屏）

- [ ] **Step 1: 写失败测试**

```tsx
it('每张卡各自请求各自落位——快的先出来，不等慢的', async () => {
  // spec D5 裁决二。一个端点返回全部统计的话，第一张卡也要等最慢的
  // 那个领域算完。这条用例让「合并成一个请求」的实现变红。
  let resolveSlow: (v: Response) => void = () => {}
  statsResponders['slow'] = () => new Promise<Response>((r) => { resolveSlow = r })
  renderDashboard()
  // fast 那张卡已经有数字了，slow 那张还在骨架屏
  await waitFor(() => expect(screen.getByText('20,017')).toBeTruthy())
  expect(screen.getByTestId('domain-card-slow-skeleton')).toBeTruthy()
})

it('骨架屏先出，不是白屏', async () => {
  // 所有 stats 都 pending 时，领域名和卡片轮廓已经在了。
})

it('一张卡统计失败时只有它显示失败，别的卡照常', async () => {
  // 一个领域的图谱查不通，不该让整个看板变成一个错误页。
  statsStatus['broken'] = 500
  renderDashboard()
  await waitFor(() => expect(screen.getByText(/统计失败/)).toBeTruthy())
  expect(screen.getByText('20,017')).toBeTruthy()  // 别的卡还在
})

it('数字带千分位', async () => {
  // 1204883 读不出来是一百二十万还是十二万。
  await waitFor(() => expect(screen.getByText('1,204,883')).toBeTruthy())
})

it('按组织分组，没挂组织的单独一组', async () => {
  // 没挂组织是合法状态（存量租户），不是错误。
})

it('待办数不为零时是可点击的，点了去审核页', async () => {
  // 看板的价值在于「看到之后能立刻去做」。一个只能看的数字是死的。
})

it('一个领域都没有时说清楚该做什么', async () => {
  // 新部署的第一屏。「暂无数据」等于什么都没说。
  domainsResponse = { domains: [] }
  renderDashboard()
  await waitFor(() => expect(screen.getByText(/还没有任何领域/)).toBeTruthy())
})
```

补全成可运行用例。

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

`DomainCard` 自己 `useEffect` 拉 `stats`，三态：`loading` 出骨架屏、`error` 出
「统计失败」+ 重试按钮、`ok` 出四个数字。`DashboardPage` 只负责拉清单、按 `org_id`
分组、渲染卡片——**不代替卡片取数**。

数字用 `toLocaleString()`；四个格子用 `font-variant-numeric: tabular-nums`
（Tailwind `tabular-nums`）。

- [ ] **Step 5: 变异**

```bash
# 变异 A：DashboardPage 改成一次性拉全部 stats 再渲染
#   预期红：test「每张卡各自请求各自落位」
# 变异 B：单张卡失败时整页显示错误
#   预期红：test「一张卡统计失败时只有它显示失败」
# 变异 C：去掉 toLocaleString
#   预期红：test「数字带千分位」
# 变异 D：待办数渲染成纯文本
#   预期红：test「待办数不为零时是可点击的」
```

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/DashboardPage.tsx frontend/src/admin/DomainCard.tsx frontend/src/admin/dashboard.test.tsx
git commit -m "feat(admin): 看板，每个领域一张卡各自落位不等最慢的那个"
```

---

## Task 6: 收尾验证

- [ ] **Step 1: 后端全量 + 前端全量 + 类型检查**

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider > "$TEMP/full.txt" 2>&1
tail -5 "$TEMP/full.txt"
```
```
cd frontend && npx vitest run src && npx tsc --noEmit
```

- [ ] **Step 2: 重启前后端并手工走一遍**

九项：

1. 登录 admin → 落在看板上，不是别的页
2. 看板上每个领域一张卡，数字带千分位
3. 用一个数据量大的租户 → 那张卡先出骨架屏再落数字，别的卡不受影响
4. 把 Neo4j 停掉 → 卡片显示「统计失败」+ 重试按钮，其余卡照常
5. 侧边栏是六组，没有「明细查询」那个孤儿组
6. 挨个点开六组里的每一项，确认都能打开（占位页也算打开）
7. 访问旧路径 `/admin/terms` → 一跳落到 `/admin/browse/terms`
8. 访问 `/admin/model/ontology` → 一跳落到 `/admin/ontology/ontology`
9. admin 未切租户时打开看板 → **正常显示**，不被「请先选择一个租户」挡住

第 3、4、9 项是这个计划的核心，**不能跳过**。

---

## Self-Review

**1. Spec coverage**

| Spec 要求 | 落在哪 |
|---|---|
| D5 全部实时算 | Task 2 / Task 3 |
| D5 裁决一：边计数从节点侧发起 | Task 1 |
| D5 裁决二：逐卡片独立请求各自落位 | Task 3（两个端点）+ Task 5 |
| 裁决补充：只显示有权访问的领域 | Task 3 |
| D8 实体明细归入结果预览 | Task 4 |
| 六模块结构 | Task 4 |
| 路由变化表 + 垫片一跳直达 | Task 4 Step 2/3 |
| 看板是登录落地页 | Task 4 Step 4 |
| 数据库导入 / 属性冲突 / 脏边 / 图谱预览 / 报错明细 | 占位页（Task 4 Step 5），实现在阶段四~六 |

**2. Placeholder scan**：占位页是**有意为之的功能**（阶段四~六交付前的入口），
不是计划的占位符——它的内容、必须给出替代路径这一条都写明了。
用例骨架同前两份计划，点名了照抄哪个文件。

**3. Type consistency**

- `count_relation_edges_for_tenant(*, tenant_id) -> int`：Task 1 定义，Task 2 消费。
- `TenantStats` 五个字段：Task 2 定义，Task 3 返回，Task 5 消费，一致。
- `ADMIN_ROUTES` 的七个新 key：Task 4 定义，Task 5 与阶段四~六消费。

---

## 已知的执行前不确定项

1. **`ingested_documents` 在哪个连接下、有没有现成计数函数**（Task 2 Step 2）：先 grep。
2. **`/admin` 的 index route 当前指向哪**（Task 4 Step 4）：先读 `App.tsx`。
3. **既有用例里硬编码的旧路径**（Task 4 Step 6）：逐条改成新路径，不要保留旧值。
4. **`EmptyState` 的 props 形状**（Task 4 Step 5）：`AdminLayout.tsx` 里在用，照抄。
