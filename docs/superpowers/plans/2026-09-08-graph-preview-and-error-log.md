# 图谱预览与报错明细 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 能以任一实体为中心看一张邻域图；散落在四处的失败收进一个可查的地方。

**Architecture:** 图谱预览用已有的 `query_subgraph`（一跳/两跳可配）+ 已装的 `sigma` / `graphology`，上限 300 节点且**截断必须说出口**（spec D6）。报错明细只收「用户能看懂、能纠正」的三类（spec D7），不做通用异常收集器——那种表本身就是个静默失败。

**Tech Stack:** FastAPI · aiosqlite · Neo4j · React 18 + TypeScript · sigma@3 · graphology · vitest

**Spec:** `docs/superpowers/specs/2026-09-08-interaction-redesign-design.md`

**Depends on:** `docs/superpowers/plans/2026-09-08-nav-restructure-and-dashboard.md`（两个页面要落在新导航的「结果预览」和「日志明细」组里，占位页在那里建好了）。与阶段四（审核拆分）**无依赖，可并行**。

## Global Constraints

同 `2026-09-08-multi-persona-foundation.md` 的十二条，逐字适用。额外两条：

13. **截断必须说出口。** 「已截断，画了 300 / 1013」，不是默默少画。这是本计划最容易退化成静默失败的地方。
14. **不做通用异常收集器。** 写入点漏一处就永远看不见，而用户会把「报错明细是空的」读成「没出错」。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/api/admin_graph_preview_routes.py`（新） | 邻域图数据端点 |
| `frontend/src/admin/DataGraphPage.tsx`（新） | 图谱预览页（搜实体 → 画邻域） |
| `frontend/src/admin/dataGraph/NeighborhoodGraph.tsx`（新） | sigma 渲染，懒加载 |
| `app/memory/schema.py`（改） | `qa_diagnostics.outcome` 加列 |
| `app/graphrag/etl_skipped_rows.py`（新） | ETL 跳过行落库，跨 run 可查 |
| `app/api/admin_error_log_routes.py`（新） | 报错明细三个来源 |
| `frontend/src/admin/ErrorLogPage.tsx`（新） | 报错明细页（三个分页 + 各自的修复动作） |

---

## Task 1: 邻域图数据端点

**Files:**
- Create: `app/api/admin_graph_preview_routes.py`
- Modify: `app/main.py`（挂载）
- Test: `tests/api/test_admin_graph_preview_routes.py`

**Interfaces:**
- Consumes: `GraphWriteProtocol.query_subgraph(node_key, *, tenant_id, chain_query_relation_types: set[str]) -> list[dict[str, Any]]`（`app/graphrag/neo4j_client.py:681`）
- Produces:
  - `GET /api/admin/{tenant_id}/graph-preview/{node_key}` → `{center, nodes: [{node_key, standard_name, term_type}], edges: [{source, relation_type, target}], truncated: bool, total_nodes: int, shown_nodes: int}`

**关键约束**：`query_subgraph` 的 `chain_query_relation_types` **必填、没有默认值**——
它的 docstring 明确说了给默认值会让注入路径没接好时悄悄按错的关系集合查两跳。
本端点必须从 `tenant_relation_types` 里读出该租户放开链式查询的关系类型再传进去，
**不许传空集当默认**。

- [ ] **Step 1: （已核对，照抄下面这段）**

权威算法在 `app/api/agent_routes.py:136-147`，逐字照抄：

```python
    confirmed_relation_type_defs = await list_relation_types(
        review_conn, tenant_id, status="confirmed"
    )
    # 子图查询按这一份来。跟 /agent/chat 用的是同一段代码——自己另写一份的话，
    # 问答里能走两跳的关系和预览里能走两跳的关系会不一样，而用户会拿这两处
    # 互相印证：他在预览里看到 A 两跳能到 C，回去问却答不出来。
    chain_query_relation_types = {
        rt.relation_type for rt in confirmed_relation_type_defs if rt.allow_chain_query
    }
```

`list_relation_types` 的 import 照抄 `agent_routes.py` 顶部那一行。
`qa_routes.py:65` 也有同一段——这已经是第三份拷贝了，若三处能收敛成一个
`resolve_chain_query_relation_types(conn, tenant_id)` 帮手函数更好，
但那是可选的重构，不是本任务的交付物。

- [ ] **Step 2: 写失败测试**

```python
def test_returns_the_center_and_its_neighbours(preview_conn):
    """中心节点必须在 nodes 里。不在的话前端得自己补一个，
    而它补出来的那个没有 term_type——图上会出现一个没有颜色的孤点。"""


def test_truncates_at_the_cap_and_says_so(preview_conn):
    """超过上限时 truncated=true，且 total_nodes 是真实总数不是上限。

    这是整个功能里最容易退化成静默失败的地方：默默少画的话，用户对着
    一张画了 300 个邻居的图下「这个实体只连了 300 个东西」的结论——
    而它连着 1013 个。
    """
    body = _preview(preview_conn, neighbours=1013).json()
    assert body["truncated"] is True
    assert body["total_nodes"] == 1014  # 1013 个邻居 + 中心
    assert body["shown_nodes"] == 300


def test_does_not_claim_truncation_when_everything_fits(preview_conn):
    """没截断时 truncated=false。恒为 true 的实现会让每张图都挂着
    一句吓人的提示，用户很快就不看它了。"""


def test_edges_reference_only_nodes_that_are_in_the_payload(preview_conn):
    """截断之后，指向被截掉的节点的边也要一起去掉。留着的话，
    sigma 会因为找不到端点而抛异常，整张图变白。

    这条用例在「先截节点、忘了截边」的实现下必红。
    """
    body = _preview(preview_conn, neighbours=1013).json()
    keys = {n["node_key"] for n in body["nodes"]}
    for edge in body["edges"]:
        assert edge["source"] in keys
        assert edge["target"] in keys


def test_a_nonexistent_node_key_is_a_404(preview_conn):
    """不存在的实体返回 404，不是一张空图。空图看起来像
    「这个实体一条关系都没有」。"""


def test_the_endpoint_is_tenant_scoped(preview_conn):
    assert _preview(preview_conn, tenant="别人的", role="member").status_code == 403


def test_chain_relation_types_come_from_the_tenant_config(preview_conn):
    """两跳走哪些关系必须来自 tenant_relation_types，不是写死的一组。

    写死的话，预览里能走两跳的关系和问答里能走两跳的关系不一样，
    而用户会拿这两处互相印证。
    """
```

- [ ] **Step 3: 跑测试确认它红 → 写实现 → 绿**

实现要点（截断逻辑）：

```python
#: 一张图最多画几个节点。
#:
#: 300 是浏览器里 forceatlas2 布局还能在一两秒内收敛、且人眼还能分辨的量级。
#: demo 那张图里「产品:Beer」连着 1013 条边——不设上限的话这一页会卡死。
#:
#: 超出时**说出来**（truncated + total_nodes），不是默默少画：默默少画的话
#: 用户会对着一张 300 个邻居的图下「这个实体只连了 300 个东西」的结论。
GRAPH_PREVIEW_NODE_CAP = 300
```

```python
    rows = await graph_client.query_subgraph(
        node_key, tenant_id=tenant_id, chain_query_relation_types=chain_types
    )
    # 先收集全部节点算出真实总数，再截断——先截再数的话 total_nodes
    # 恒等于上限，那句「共 1013 个」就永远说不出来。
    all_nodes = _collect_nodes(rows, center)
    kept = all_nodes[:GRAPH_PREVIEW_NODE_CAP]
    kept_keys = {n["node_key"] for n in kept}
    # 边也要跟着截：指向被截掉节点的边留着的话，sigma 找不到端点会抛异常，
    # 整张图变白——比少画几个节点糟得多。
    edges = [e for e in _collect_edges(rows) if e["source"] in kept_keys and e["target"] in kept_keys]
```

**中心节点必须优先保留**：`_collect_nodes` 把 center 放在第一位。

- [ ] **Step 4: 变异**

```bash
# 变异 A：total_nodes 用截断后的数量
#   预期红：test_truncates_at_the_cap_and_says_so
# 变异 B：truncated 恒为 False
#   预期红：test_truncates_at_the_cap_and_says_so
# 变异 C：只截节点不截边
#   预期红：test_edges_reference_only_nodes_that_are_in_the_payload
# 变异 D：chain_query_relation_types 传一个写死的集合
#   预期红：test_chain_relation_types_come_from_the_tenant_config
```

- [ ] **Step 5: 重启后端 + 提交**

```bash
git add app/api/admin_graph_preview_routes.py tests/api/test_admin_graph_preview_routes.py app/main.py
git commit -m "feat(api): 实体邻域图端点，超过 300 个节点时把截断说出来"
```

---

## Task 2: 图谱预览页

**Files:**
- Create: `frontend/src/admin/DataGraphPage.tsx`
- Create: `frontend/src/admin/dataGraph/NeighborhoodGraph.tsx`
- Modify: `frontend/src/App.tsx`（换掉阶段三的占位页）
- Modify: `frontend/src/admin/TermsPage.tsx`（每行加一个「看图」入口）
- Test: `frontend/src/admin/dataGraph.test.tsx`

**Interfaces:**
- Consumes: Task 1 的端点
- Produces: `DataGraphPage`、`NeighborhoodGraph`（`React.lazy` 懒加载——sigma 的包不小，
  别的页面不该为它付加载成本；照抄 `OntologyGraphPage.tsx:10` 那个 `lazy(() => import(...))` 写法）

- [ ] **Step 1: 写失败测试**

```tsx
it('搜一个实体就画出它的邻域', async () => {})

it('截断时把「画了 300 / 共 1013」写在图旁边', async () => {
  // 这是这一页最重要的一句话。没有它，用户会对着一张不完整的图
  // 下一个完整的结论。
  await waitFor(() => expect(screen.getByText(/300\s*\/\s*1,013/)).toBeTruthy())
})

it('没截断时不显示那句话', async () => {
  // 恒显示的话，用户很快就不看它了——而它在真的截断时是关键信息。
})

it('搜一个不存在的实体时说清楚，不是画一张空图', async () => {
  // 空图看起来像「这个实体一条关系都没有」。
})

it('从实体明细点「看图」会带着那个实体跳过来', async () => {
  // 这一页最自然的入口不是搜索框，是「我正在看这个实体，想看看它连着什么」。
})

it('加载中显示骨架，不是白屏', async () => {})

it('图渲染失败时说出来并保留搜索框', async () => {
  // sigma 在某些环境下会失败（WebGL 不可用）。整页白掉的话，
  // 用户连重新搜一个都做不到。
})
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

`NeighborhoodGraph` 用 `graphology` 建图 + `graphology-layout-forceatlas2` 布局 +
`sigma` 渲染。节点按 `term_type` 上色，中心节点加粗。

**测试环境里 sigma 跑不起来**（jsdom 没有 WebGL）——所以：
`DataGraphPage` 负责取数、状态、截断提示，`NeighborhoodGraph` 只负责画。
测试只测 `DataGraphPage`，把 `NeighborhoodGraph` mock 掉。这个切分不是为了好测，
是因为「截断提示」这件事属于数据层不属于渲染层——它在 WebGL 不可用时也必须出现。

- [ ] **Step 5: 变异**

```bash
# 变异 A：截断提示恒显示
#   预期红：test「没截断时不显示那句话」
# 变异 B：截断提示不渲染
#   预期红：test「截断时把「画了 300 / 共 1013」写在图旁边」
# 变异 C：404 时渲染空图
#   预期红：test「搜一个不存在的实体时说清楚」
```

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/DataGraphPage.tsx frontend/src/admin/dataGraph/NeighborhoodGraph.tsx frontend/src/admin/dataGraph.test.tsx frontend/src/App.tsx frontend/src/admin/TermsPage.tsx
git commit -m "feat(admin): 图谱预览页，以实体为中心画邻域并说明截断"
```

---

## Task 3: `qa_diagnostics.outcome`

**Files:**
- Modify: `app/memory/schema.py:22-34`
- Modify: 写 `qa_diagnostics` 的那处（grep `INSERT INTO qa_diagnostics`）
- Test: `tests/memory/test_qa_diagnostics.py`（新建或追加）

**Interfaces:**
- Produces: `qa_diagnostics.outcome TEXT NOT NULL DEFAULT 'answered'`，取值
  `answered` / `no_match` / `error`

**今天的状况**：`qa_diagnostics` 记了问题、答案、来源、工具结果，**但没有错误字段**
——答不出来和答得好长得一模一样。报错明细的「问答未命中」页全靠这一列。

- [ ] **Step 1: 先查清楚「答不出来」在管线里长什么样**

Run: `grep -rn "matched_count\|没有找到\|无法回答\|no_match" app/agent/ app/api/agent_routes.py | head -20`

判据必须来自管线里已经存在的信号，**不要新发明一个**。
候选：工具返回 `matched_count: 0`、检索结果为空、LLM 明确说不知道。
选哪一个（或哪几个的组合）读完源码再定，并把理由写进代码注释。

- [ ] **Step 2: 写失败测试**

```python
def test_a_normal_answer_is_recorded_as_answered():
    """默认值是 answered。默认成 no_match 的话，报错明细上线第一天
    就会显示历史上每一次问答都失败了。"""


def test_an_answer_with_no_matching_data_is_recorded_as_no_match():
    """这一条是整个功能的支点。判据来自管线里已有的信号
    （见 Step 1 的调查结论），不是新发明的。"""


def test_a_pipeline_error_is_recorded_as_error():
    """报错和未命中要分开：未命中是「本体里没有这个概念，去建模」，
    报错是「系统坏了，去看日志」——两种完全不同的修复动作。"""


def test_existing_rows_read_back_as_answered():
    """加列之前的历史记录没有这一列。DEFAULT 'answered' 让它们读回来
    是「答过」——那是最接近事实的假设（它们当时确实产出了答案）。"""
```

- [ ] **Step 3 – Step 5: 红 → 实现 → 绿 → 变异**

加列用 `app/db_migrations.py:6` 的 `add_column_if_missing(conn, table="qa_diagnostics", column="outcome", ddl="TEXT NOT NULL DEFAULT 'answered'")`——已核对存在，`terms_store.py:215-220` 是现成用例。

```bash
# 变异 A：DEFAULT 改成 'no_match'
#   预期红：test_existing_rows_read_back_as_answered
# 变异 B：no_match 和 error 合并成一个值
#   预期红：test_a_pipeline_error_is_recorded_as_error
```

- [ ] **Step 6: 提交**

```bash
git add app/memory/schema.py app/api/agent_routes.py tests/memory/test_qa_diagnostics.py
git commit -m "feat(logs): 问答诊断记下这次是答出来了、没命中还是报错了"
```

---

## Task 4: ETL 跳过行落库

**Files:**
- Create: `app/graphrag/etl_skipped_rows.py`
- Modify: `app/graphrag/schema_etl.py:149-160`（`_record_skipped_row`）
- Test: `tests/graphrag/test_etl_skipped_rows.py`

**Interfaces:**
- Produces:
  - `async def ensure_etl_skipped_rows_schema(conn) -> None`
  - `async def record_skipped_rows(conn, *, tenant_id, run_id, rows: list[SkippedRow]) -> None`——批量写
  - `async def list_skipped_rows(conn, *, tenant_id, limit=None, offset=0) -> list[dict[str, Any]]`
  - `async def count_skipped_rows(conn, *, tenant_id) -> int`

**今天的状况**：跳过行只在**单次 run 的报告**里（`ReportSkippedRow` dataclass，
可下载 CSV），跨 run 查不了。上周导入跳了 127 行这件事，今天没有任何地方能查到。

- [ ] **Step 1: 写失败测试**

```python
def test_skipped_rows_survive_the_run_that_produced_them():
    """跨 run 可查是这张表存在的全部理由。上周跳的 127 行今天要还能查到。"""


def test_rows_are_scoped_to_the_tenant():
    """两个租户各跑一次导入，各自只看到自己的。"""


def test_batch_write_keeps_row_numbers_and_reasons():
    """行号和原因都要留着——「第 88 行 价格非数字」是用户能据此去改表格的
    全部信息。只留原因不留行号的话，他得在两万行里自己找。"""


def test_recording_an_empty_batch_writes_nothing_and_does_not_error():
    """一行没跳时不该在库里留下任何东西，也不该报错。
    绝大多数成功的导入都会走这条路径。"""


def test_the_same_run_recorded_twice_does_not_double_up():
    """重跑同一个 run_id 时替换而不是追加。追加的话，重试一次导入
    就会让跳过行数翻倍——而用户以为问题变严重了。"""
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

DDL：

```sql
CREATE TABLE IF NOT EXISTS etl_skipped_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    label       TEXT NOT NULL,
    source_file TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_etl_skipped_rows_tenant
    ON etl_skipped_rows (tenant_id, id);
```

`schema_etl.py` 里在 run 结束时把 `report.skipped_rows` 整批写进去。
**不要在 `_record_skipped_row` 里逐条写库**——那是每行一次事务，两万行的导入会被拖垮。

- [ ] **Step 5: 变异 + 提交**

```bash
# 变异 A：重跑同一 run_id 时追加
#   预期红：test_the_same_run_recorded_twice_does_not_double_up
# 变异 B：不存 row_number
#   预期红：test_batch_write_keeps_row_numbers_and_reasons
# 变异 C：list 去掉 tenant_id 过滤
#   预期红：test_rows_are_scoped_to_the_tenant
```

```bash
git add app/graphrag/etl_skipped_rows.py app/graphrag/schema_etl.py tests/graphrag/test_etl_skipped_rows.py app/main.py
git commit -m "feat(logs): ETL 跳过的行落库，上周跳的 127 行今天还能查"
```

---

## Task 5: 报错明细 API 与页面

**Files:**
- Create: `app/api/admin_error_log_routes.py`
- Create: `frontend/src/admin/ErrorLogPage.tsx`
- Modify: `frontend/src/App.tsx`（换掉阶段三的占位页）
- Test: `tests/api/test_admin_error_log_routes.py`
- Test: `frontend/src/admin/errorLog.test.tsx`

**Interfaces:**
- Produces:
  - `GET /api/admin/{tenant_id}/errors/documents` → `ingestion_jobs` 里 status='failed' 的（`last_error` 这一列已经在，`app/ingestion/ingestion_queue.py:41`）
  - `GET /api/admin/{tenant_id}/errors/etl-rows` → Task 4 的跳过行
  - `GET /api/admin/{tenant_id}/errors/qa` → Task 3 的 `outcome != 'answered'` 那些
  - `GET /api/admin/{tenant_id}/errors/counts` → `{documents, etl_rows, qa}`

**三个来源各带一个修复动作**（spec D7）：

| 分页 | 一行长什么样 | 修复动作 |
|---|---|---|
| 文档失败 | `xx.pdf` · OCR 超时 · 2 小时前 | **重试** |
| 表格跳行 | `商品表.xlsx` 第 88 行 · 价格非数字 | **下载这批**（CSV） |
| 问答未命中 | 「库存多少」· 无匹配 · 3 天前 | **去建模**（跳本体结构页） |

- [ ] **Step 1: 写失败测试**

```python
def test_each_source_returns_only_its_own_failures(error_conn):
    """三个来源各造两条，逐个端点断言它拿到的正是自己那两条。"""


def test_successful_documents_do_not_appear(error_conn):
    """status='completed' 的不该出现。批次里必须同时有成功和失败的——
    全是失败的话，「返回全部」的实现也能变绿。"""


def test_answered_questions_do_not_appear_in_the_qa_page(error_conn):
    """outcome='answered' 的不出现。这一页是「答不出来的那些」。"""


def test_counts_endpoint_returns_all_three_keys_including_zeroes(error_conn):
    """空的那一类是 0，不是 key 不存在。"""


def test_every_page_is_tenant_scoped(error_conn):
    """三个端点逐个断言 403。"""


def test_the_document_error_carries_the_message_not_just_a_flag(error_conn):
    """last_error 的内容要带出来。只说「失败了」的话，用户不知道
    是 OCR 超时还是文件格式不支持——两者的处理完全不同。"""
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

- [ ] **Step 5: 前端**

三个分页 + 角标，每页的修复动作按上表。

「重试」调既有的 `app/ingestion/ingestion_queue.py:227` 的
`retry_job(conn, job_id, *, tenant_id)`（已核对存在）。若它没有对应的 HTTP 端点，
在本任务里补一个薄路由包它——**不要重写重试逻辑**，那个函数里对「哪些状态允许重试」
的判断是有依据的（见它的 docstring）。

「下载这批」复用 `admin_schema_etl_routes.py:361` 那个 CSV 下载的形状。

「去建模」是一个链接，带上那个问题作为查询参数，落到本体结构页。

- [ ] **Step 6: 前端测试**

```tsx
it('三个分页都在，角标各不相同', async () => {})

it('文档失败那一行把具体错误写出来', async () => {
  // 「OCR 超时」而不是「失败」。
})

it('问答未命中那一行能一键跳到本体结构页', async () => {})

it('某一类为 0 时那一页说清楚，不是空白', async () => {
  // 「没有失败的文档」而不是一片空白——空白让人以为没加载出来。
})

it('某一个来源拉取失败只影响那一页', async () => {})
```

- [ ] **Step 7: 变异**

```bash
# 变异 A：文档端点不按 status 过滤
#   预期红：test_successful_documents_do_not_appear
# 变异 B：qa 端点不按 outcome 过滤
#   预期红：test_answered_questions_do_not_appear_in_the_qa_page
# 变异 C：文档失败只返回一个布尔标志不带 last_error
#   预期红：test_the_document_error_carries_the_message_not_just_a_flag
# 变异 D：counts 只返回非零的 key
#   预期红：test_counts_endpoint_returns_all_three_keys_including_zeroes
```

- [ ] **Step 8: 重启后端 + 提交**

```bash
git add app/api/admin_error_log_routes.py tests/api/test_admin_error_log_routes.py frontend/src/admin/ErrorLogPage.tsx frontend/src/admin/errorLog.test.tsx frontend/src/App.tsx
git commit -m "feat(admin): 报错明细三个来源各带一个修复动作"
```

---

## Task 6: 收尾验证

- [ ] **Step 1: 后端全量 + 前端全量 + 类型检查**

- [ ] **Step 2: 重启前后端并手工走一遍**

八项：

1. 实体明细里找一个边少的实体，点「看图」→ 画出来了，**没有**截断提示
2. 找 demo 里的「产品:Beer」（1013 条边）→ 画出来了，**有**「画了 300 / 共 1,013」
3. 那张图上随便点一个节点 → 能跳到它的实体详情
4. 搜一个不存在的名字 → 说「找不到这个实体」，不是空图
5. 故意传一个坏文件走文档导入 → 报错明细的「文档失败」页多一条，**带具体错误**
6. 故意在表格里放一行价格非数字 → 「表格跳行」页多一条，带行号和原因
7. 在前台问一个本体里没有的东西 → 「问答未命中」页多一条，点「去建模」能跳过去
8. 三类都为 0 的干净租户 → 三页各说「没有失败的 X」，不是三片空白

第 2 项是本计划的核心承诺，**不能跳过**。

---

## Self-Review

**1. Spec coverage**

| Spec 要求 | 落在哪 |
|---|---|
| D6 以实体为中心的邻域图，1–2 跳 | Task 1 + Task 2 |
| D6 上限 300 节点 | Task 1 的 `GRAPH_PREVIEW_NODE_CAP` |
| D6 截断必须说出口 | Task 1 的 `truncated`/`total_nodes` + Task 2 的提示 |
| D7 只收用户能纠正的三类 | Task 5 |
| D7 每条带修复动作 | Task 5 Step 5 的三个动作 |
| D7 明确不做通用异常收集器 | 无任务，spec 已说明理由 |
| §3 `qa_diagnostics.outcome` 加列 | Task 3 |
| ETL 跳过行跨 run 可查 | Task 4 |

**2. Placeholder scan**：Task 2 / Task 5 的用例是骨架 + 理由，按仓库既有夹具补全。
Task 5 里「重试端点可能不存在」给了明确的处理办法（不新建，改成一句说明），
不是 TODO。

**3. Type consistency**

- `GRAPH_PREVIEW_NODE_CAP`：Task 1 定义，Task 2 的用例引用同一个数字（300）。
- 邻域图回包七个字段：Task 1 定义，Task 2 消费。
- `qa_diagnostics.outcome` 三个取值：Task 3 定义，Task 5 的 qa 端点按 `!= 'answered'` 过滤。
- `list_skipped_rows` / `count_skipped_rows`：Task 4 定义，Task 5 消费。

---

## 已知的执行前不确定项

只剩两条，且都需要判断而不只是查找：

1. **「答不出来」在管线里的信号**（Task 3 Step 1）：读完源码再定判据，把理由写进注释。
   **不要新发明一个信号。**
2. **`retry_job` 有没有现成的 HTTP 端点**（Task 5 Step 5）：有就用，没有就补一个薄路由包它。

已在写计划时核对完毕、结论写进对应 Step 的：`chain_query_relation_types` 的算法
（`agent_routes.py:136-147`）、`retry_job` 的存在（`ingestion_queue.py:227`）、
`SkippedRow` 的四个字段（`label` / `source_file` / `row_number` / `reason`，
`schema_etl.py:103-107`，DDL 已按它写）、jsdom 下 sigma 不可用的处理（Task 2 Step 4）。
