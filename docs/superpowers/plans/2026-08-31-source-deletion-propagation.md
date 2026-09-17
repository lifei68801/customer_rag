# 源端删除的传播 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ETL 重跑后，`terms` 和 Neo4j 只包含源数据当前存在的实体与关系——源里删掉的行，图谱里也消失。

**Architecture:** 实体走 mark-and-sweep，按 `term_type` 圈定范围、只扫 `source='etl'` 的行；扫除集合在 Spec 2 已有的预检第一遍里算出来，所以安全阀触发时"整轮零改动"是结构性保证。关系走"先写新边、再扫陈旧边"，按 `source` + `recorded_at < 本次运行时间` 圈定。

**Tech Stack:** Python 3.12、aiosqlite、Neo4j（Cypher）、FastAPI（multipart 表单）、React + Tailwind、pytest + anyio。

**Spec:** [docs/superpowers/specs/2026-08-30-source-deletion-propagation-design.md](../specs/2026-08-30-source-deletion-propagation-design.md)

## Global Constraints

- sweep 只删除 `source = 'etl'` 的实体行；`manual`/`review`/`unknown` 永不被 ETL 清理。
- 关系的删除必须在所有关系写入**之前**全部完成，不能逐映射先删后写。**本计划用"先写后扫"满足这条约束的意图**——见下面"对 spec 的修正一"。
- sweep 只删 `terms` 行，不删 `term_edits` 行（Spec 3 落地后；本计划不涉及）。
- 删除数量必须出现在运行报告里，**零删除时也要出现**。
- 安全阀触发时整轮零改动，不做部分清理。
- 测试基线 **1454 passed**。每个任务结束时全量必须 `0 failed`。
- **pytest 打完 summary 会卡在 teardown**（aiosqlite 工作线程非 daemon）。跑测试一律用：
  `PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1; grep -E "passed|failed" /tmp/o.txt | tail -2`
  退出码 124 是预期的，不是失败。
- 前端**没有测试框架**（无 vitest/jest），只有 `cd frontend && npx tsc --noEmit` 和 `npx vite build`。
- 注释和文档字符串用中文，跟现有代码一致。

## 用户已拍板的两个取舍（不要重新评估）

1. **关系删除用"先写新边、再扫陈旧边"**，不用 spec 原文的"先全删再全写"。理由：ETL 数据量大、窗口长，中途失败会留下"边被删光、实体还在"的图谱；先写后扫时任何时刻图谱都是完整的，最坏情况只是新旧共存，下次重跑自愈。
2. **`dry_run` 和 `allow_large_sweep` 做成 API 字段 + 前端勾选框**，不只做 CLI 开关。理由：ETL 实际是从管理后台跑的，安全阀一旦触发而后台无法放行，操作员就卡死了。

## 三处对 spec 的修正（写计划时实读代码得出，实施时以本节为准）

**修正一 · "关系全删必须先于全写"这条约束改为用"先写后扫"实现。**
spec 的 Global Constraints 写的是"关系的删除必须在所有关系写入之前全部完成"。这条约束的**目的**是防止"多个关系映射共享同一源文件时，后一个映射的删除抹掉前一个刚写的边"（demo 配置里五条关系全部来自 `soft_drink_sales.xlsx`）。

"先写后扫"同样满足这个目的，而且更强：扫除条件带 `recorded_at < 本次运行时间`，本次写的边一律不会被扫掉，无论有多少映射共享源文件。原约束的字面（"删必须先于写"）不再成立，但它保护的东西被更好地保护了。**测试仍然必须钉住"多映射共享源文件后五种关系都在"这条断言。**

**修正二 · spec 的"孤儿边"风险不存在。**
spec 的未决风险说"`delete_term_node` 的行为需要确认（是否 DETACH DELETE）"。实读 `app/graphrag/neo4j_client.py:191-195`：

```
_DELETE_TERM_NODE_QUERY = """
MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})
OPTIONAL MATCH (a:Term)-[:ALIAS_OF]->(t)
DETACH DELETE t, a
"""
```

已经是 `DETACH DELETE`，并且连别名节点一起删。删实体时它的边一并消失。**这条风险已解决**，但计划仍要求一条断言把它钉住（Task 2）。

**修正三 · 安全阀只作用于实体，不单独作用于关系。**
spec 的安全阀例子只讲实体。本计划明确：**安全阀只检查实体 sweep**。理由——最常见的事故形态（传错文件、导出被截断）必然先反映在实体上，而实体阀触发时整轮零改动、根本走不到关系写入。给关系再加一个独立阈值只会增加一个需要单独调参的旋钮，挡不住实体阀挡不住的东西。

## 本计划的关键设计洞察

Spec 2 的预检第一遍（`scan_entity_node_keys`）**已经**持有每个 mapping 的全部 `node_key` —— `seen` 字典的键就是。所以：

- sweep 集合 = `该 term_type 下 source='etl' 的现有 node_key` − `本次算出的 node_key`
- 这个差集可以在**任何写入发生之前**算出来
- 安全阀因此和 `DuplicateNodeKeyError` 走同一条路径：预检阶段 raise，零写入是结构性的，不是靠"记得回滚"

暴露 `node_keys` 不增加任何内存——它就是 `seen` 的键集合，用户拍板的"第一遍只驻留键"完全不受影响。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `app/graphrag/terms_store.py`（改） | 新增按 `term_type` + `source='etl'` 圈定的 node_key 查询，和按 node_key 批量删除。 |
| `app/graphrag/etl_projection.py`（改） | `KeyScanResult` 暴露 `node_keys`（零额外内存）。 |
| `app/graphrag/neo4j_client.py`（改） | 新增 `delete_stale_relations_by_source`（按 source + recorded_at 早于本次运行）。 |
| `app/graphrag/schema_etl.py`（改） | `ETLRunReport` 新增删除计数字段；`run_schema_etl` 在预检阶段算 sweep 集合与安全阀，写入后执行 sweep；`SchemaEtlGraphProtocol` 扩容。 |
| `app/api/admin_schema_etl_routes.py`（改） | 跑批入口接受 `dry_run` / `allow_large_sweep` 两个表单字段并透传。 |
| `frontend/src/admin/SchemaEtlPage.tsx`（改） | 上传表单加两个勾选框。 |
| `tests/graphrag/test_terms_store.py`（改） | 新查询与批量删除的单测。 |
| `tests/graphrag/test_schema_etl.py`（改） | 源端删除传播、人工创建不被波及、关系全量替换、多映射共享源、安全阀零改动、dry-run。 |
| `tests/api/test_admin_schema_etl_routes.py`（改） | 两个表单字段的透传。 |

---

## Task 1: 实体 sweep 的存储层

**Files:**
- Modify: `app/graphrag/terms_store.py`
- Modify: `app/graphrag/etl_projection.py`
- Test: `tests/graphrag/test_terms_store.py`

**Interfaces:**
- Consumes: `terms` 表已有的 `tenant_id` / `node_key` / `term_type` / `source` 列（`source` 默认 `'unknown'`，ETL 写入时是 `'etl'`）。
- Produces:
  - `async def list_etl_node_keys_by_term_type(conn, tenant_id: str, term_type: str) -> set[str]`
  - `async def delete_terms_by_node_keys(conn, tenant_id: str, node_keys: set[str]) -> int`（返回实际删除行数）
  - `KeyScanResult.node_keys: set[str]`

**背景：** `app/graphrag/terms_store.py:356` 已有 `list_node_keys_by_term_type(conn, tenant_id, term_type) -> set[str]`，但它**不按 source 过滤**——sweep 不能用它，否则会把审核界面创建的（`source='review'`）和管理后台手工录入的（`source='manual'`）一起扫掉。新函数是它的 source 受限版本，**不要改动原函数**，它的调用方（关系端点守卫）需要的正是"全部 node_key"。

- [ ] **Step 1: 在 `tests/graphrag/test_terms_store.py` 末尾写失败的测试**

先确认文件顶部已导入的符号，补上 `list_etl_node_keys_by_term_type`、`delete_terms_by_node_keys`、`upsert_term_with_node_key`（如已导入则不重复）。

```python
async def test_list_etl_node_keys_by_term_type_excludes_manual_and_review_rows():
    """sweep 只能扫 ETL 自己写进来的行。审核界面创建的（source='review'）和
    管理后台手工录入的（source='manual'）从来就不来自这个数据源，"源里没有"
    对它们不成立，扫掉它们是数据丢失。"""
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:B", standard_name="B",
        aliases=[], term_type="产品", extra_properties={}, source="review",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:C", standard_name="C",
        aliases=[], term_type="产品", extra_properties={}, source="manual",
    )

    keys = await list_etl_node_keys_by_term_type(conn, "t1", "产品")

    assert keys == {"产品:A"}


async def test_list_etl_node_keys_by_term_type_is_scoped_to_tenant_and_type():
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="类目:X", standard_name="X",
        aliases=[], term_type="类目", extra_properties={}, source="etl",
    )
    await upsert_term_with_node_key(
        conn, tenant_id="t2", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )

    assert await list_etl_node_keys_by_term_type(conn, "t1", "产品") == {"产品:A"}


async def test_delete_terms_by_node_keys_removes_only_the_named_rows():
    conn = await _connect()
    for key in ("产品:A", "产品:B", "产品:C"):
        await upsert_term_with_node_key(
            conn, tenant_id="t1", node_key=key, standard_name=key,
            aliases=[], term_type="产品", extra_properties={}, source="etl",
        )

    removed = await delete_terms_by_node_keys(conn, "t1", {"产品:A", "产品:C"})

    assert removed == 2
    assert await list_etl_node_keys_by_term_type(conn, "t1", "产品") == {"产品:B"}


async def test_delete_terms_by_node_keys_on_empty_set_is_a_noop():
    """空集合必须是干净的空操作——绝不能退化成"没有 WHERE 条件"把整张表删了。
    这是本函数最危险的失败形态。"""
    conn = await _connect()
    await upsert_term_with_node_key(
        conn, tenant_id="t1", node_key="产品:A", standard_name="A",
        aliases=[], term_type="产品", extra_properties={}, source="etl",
    )

    removed = await delete_terms_by_node_keys(conn, "t1", set())

    assert removed == 0
    assert await list_etl_node_keys_by_term_type(conn, "t1", "产品") == {"产品:A"}
```

**实施者注意**：`upsert_term_with_node_key` 的 `source` 参数名与默认值请打开 `app/graphrag/terms_store.py` 核对。如果它没有 `source` 关键字参数，就用 `create_term` 或直接 `conn.execute` 插入带指定 source 的行来构造夹具，并在报告里说明。`_connect()` 是该测试文件已有的辅助函数。

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest tests/graphrag/test_terms_store.py -q -k "etl_node_keys or delete_terms_by_node_keys" > /tmp/o.txt 2>&1
grep -E "passed|failed|Error" /tmp/o.txt | tail -3
```

Expected: `ImportError` / `cannot import name 'list_etl_node_keys_by_term_type'`。

- [ ] **Step 3: 在 `terms_store.py` 里实现两个函数**

放在 `list_node_keys_by_term_type`（约 :356）之后：

```python
async def list_etl_node_keys_by_term_type(
    conn: aiosqlite.Connection, tenant_id: str, term_type: str
) -> set[str]:
    """该租户、该类型下、**由 ETL 写入**的全部 node_key。

    跟 list_node_keys_by_term_type 的区别只有 source 过滤，但这个区别是
    本质的：ETL 的 sweep（源里没有的实体要删掉）只能作用于 ETL 自己写进来
    的行。审核界面现场创建的（source='review'）和管理后台手工录入的
    （source='manual'）从来就不来自这个数据源，"源里没有"对它们不成立。

    不要把这个过滤加进 list_node_keys_by_term_type——那个函数服务的是关系
    写入的端点存在性守卫，它需要的正是"全部 node_key"，无论来源。
    """
    cursor = await conn.execute(
        "SELECT node_key FROM terms WHERE tenant_id = ? AND term_type = ? AND source = 'etl'",
        (tenant_id, term_type),
    )
    return {row[0] for row in await cursor.fetchall()}


async def delete_terms_by_node_keys(
    conn: aiosqlite.Connection, tenant_id: str, node_keys: set[str]
) -> int:
    """按 node_key 批量删除该租户的术语行，返回实际删除的行数。

    空集合是干净的空操作，直接返回 0——绝不能让它退化成一条没有有效 WHERE
    条件的 DELETE 把整张表清空。这是本函数最危险的失败形态，有测试钉住。

    只删 terms 行。图谱侧的节点删除由调用方另行调用 delete_term_node
    （它是 DETACH DELETE，会连边和别名节点一起清掉）。
    """
    if not node_keys:
        return 0
    keys = list(node_keys)
    placeholders = ",".join("?" * len(keys))
    cursor = await conn.execute(
        f"DELETE FROM terms WHERE tenant_id = ? AND node_key IN ({placeholders})",
        (tenant_id, *keys),
    )
    await conn.commit()
    return cursor.rowcount
```

- [ ] **Step 4: 给 `KeyScanResult` 暴露 `node_keys`**

在 `app/graphrag/etl_projection.py` 里，`KeyScanResult` 加一个字段：

```python
@dataclass(frozen=True)
class KeyScanResult:
    """第一遍扫描的产物。只保留键和行号——不保留行本身，内存上界因此
    只跟行数有关，跟行有多宽无关。

    node_keys 是本次源文件算出的全部 node_key。它就是内部 seen 字典的键
    集合，暴露出来不增加任何内存占用。sweep（源端删除传播）需要它来算
    "该 term_type 下现有的、但本次没算出来的"那个差集——而且因为它在
    第一遍就有，sweep 的安全阀可以在任何写入之前判定，"整轮零改动"是
    结构性的，不是靠回滚。
    """

    duplicate_keys: dict[str, list[int]]
    scanned_rows: int
    node_keys: set[str]
```

在 `scan_entity_node_keys` 的 `return` 里带上：

```python
    return KeyScanResult(
        duplicate_keys={k: v for k, v in seen.items() if len(v) > 1},
        scanned_rows=scanned,
        node_keys=set(seen),
    )
```

**注意**：`KeyScanResult` 是 `frozen=True` 的 dataclass，加字段会让所有构造点必须提供它。全库 grep `KeyScanResult(` 确认只有这一个构造点；`tests/graphrag/test_etl_projection.py` 里如果有断言整个对象相等的用例，需要相应更新（按实际情况改，不要削弱断言）。

- [ ] **Step 5: 跑测试确认通过，再跑全量**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `1458 passed`（1454 + 4），0 failed。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/terms_store.py app/graphrag/etl_projection.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): let the store scope node keys to ETL-written rows"
```

---

## Task 2: 实体 sweep + 安全阀 + 报告字段

**Files:**
- Modify: `app/graphrag/schema_etl.py`
- Test: `tests/graphrag/test_schema_etl.py`

**Interfaces:**
- Consumes: `list_etl_node_keys_by_term_type(conn, tenant_id, term_type) -> set[str]`、`delete_terms_by_node_keys(conn, tenant_id, node_keys) -> int`、`KeyScanResult.node_keys`（Task 1）；`delete_term_node(*, tenant_id, node_key)`（`neo4j_client.py:650`，DETACH DELETE）。
- Produces:
  - `ETLRunReport` 新增 `entities_removed: int`、`entities_removed_by_type: dict[str, int]`、`relations_removed: int`、`dry_run: bool`
  - `class SweepSafetyValveError(Exception)`
  - `run_schema_etl(..., dry_run: bool = False, allow_large_sweep: bool = False)`
  - `SchemaEtlGraphProtocol` 新增 `delete_term_node`

**关键结构（这是本任务的核心，不要改）：**

sweep 集合和安全阀都在**预检阶段**算完，在任何写入之前：

```
1. 预检：对每个 EntityMapping 跑 scan_entity_node_keys
   → 得到 duplicate_keys（Spec 2 已有）和 node_keys（Task 1 新增）
2. 有重复键 → raise DuplicateNodeKeyError（Spec 2 已有），零写入
3. 算 sweep 集合：list_etl_node_keys_by_term_type(...) - scan.node_keys
4. 安全阀：任一 term_type 的 sweep 占比 > 阈值且未放行 → raise，零写入
5. dry_run → 直接返回只填了删除计数的报告，不写不删
6. 写入实体、写入关系
7. 执行 sweep（删 terms 行 + 删 Neo4j 节点）
```

**为什么 sweep 的执行放在写入之后、而判定放在写入之前**：判定必须在写入前，才能保证阀触发时零改动；执行必须在写入后，否则先删后写会在中途留下实体缺失、关系写入的端点守卫大面积误判。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_schema_etl.py` 顶部 import 区补上 `SweepSafetyValveError`，以及 `from app.graphrag.terms_store import upsert_term_with_node_key`（若未导入）。

**同时**：`FakeGraphClient`（该文件约 :26）需要新增 `delete_term_node` 方法，否则协议不满足。改成：

```python
class FakeGraphClient:
    def __init__(self) -> None:
        self.synced: list[str] = []
        self.merged: list[tuple[str, str, str]] = []
        self.deleted_nodes: list[str] = []

    async def sync_term(self, term) -> None:
        self.synced.append(term.node_key)

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type,
        source, tenant_id, provenance, recorded_at,
    ) -> None:
        self.merged.append((subject_standard_name, object_standard_name, relation_type))

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None:
        self.deleted_nodes.append(node_key)
```

**不要删掉 `synced` / `merged` 上任何既有断言依赖的行为。**

然后在文件末尾追加：

```python
async def test_run_schema_etl_removes_entities_that_vanished_from_the_source(tmp_path):
    """源里删掉一行，重跑之后那个实体就该从 terms 和图谱里消失——数据源是
    权威的，本体是它的投影。ETL 此前只有 upsert、没有任何删除，源修正后
    得到的是新旧并存而不是修正后的状态。"""
    conn = await _confirmed_conn()
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={},
            ),
        ],
        relations=[],
    )
    path = tmp_path / "products.csv"
    path.write_text(
        "product_group_id,product_group_name\nP1,甲\nP2,乙\nP3,丙\n", encoding="utf-8"
    )
    await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )
    assert len(await list_terms(conn, "muji")) == 3

    path.write_text("product_group_id,product_group_name\nP1,甲\nP2,乙\n", encoding="utf-8")
    graph_client = FakeGraphClient()
    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert report.entities_removed == 1
    assert report.entities_removed_by_type == {"Product": 1}
    assert {t.node_key for t in await list_terms(conn, "muji")} == {"Product:P1", "Product:P2"}
    # 图谱侧也要删——delete_term_node 是 DETACH DELETE，连边和别名节点一起清。
    assert graph_client.deleted_nodes == ["Product:P3"]


async def test_run_schema_etl_sweep_never_touches_manually_created_terms(tmp_path):
    """审核界面创建的实体（source='review'）从来就不来自这个数据源，
    "源里没有"对它不成立。即使它的 term_type 由 ETL 管理，也不能被扫掉。"""
    conn = await _confirmed_conn()
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={},
            ),
        ],
        relations=[],
    )
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name\nP1,甲\n", encoding="utf-8"
    )
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:HAND", standard_name="手工产品",
        aliases=[], term_type="Product", extra_properties={}, source="review",
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_removed == 0
    assert "Product:HAND" in {t.node_key for t in await list_terms(conn, "muji")}


async def test_run_schema_etl_reports_zero_removals_explicitly(tmp_path):
    """零删除也要出现在报告里——"本次没有移除任何实体"和"根本没跑删除
    逻辑"必须能区分开。"""
    conn = await _confirmed_conn()
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={},
            ),
        ],
        relations=[],
    )
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name\nP1,甲\n", encoding="utf-8"
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_removed == 0
    assert report.entities_removed_by_type == {"Product": 0}
    assert report.relations_removed == 0


async def test_run_schema_etl_safety_valve_aborts_with_zero_changes(tmp_path):
    """一次误传的、被截断的源文件会静默清空大半个图谱，而症状要等用户提问
    答不出来才暴露。阈值把最常见的事故形态挡在门外，且触发时整轮零改动——
    不做部分清理。"""
    conn = await _confirmed_conn()
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={},
            ),
        ],
        relations=[],
    )
    path = tmp_path / "products.csv"
    path.write_text(
        "product_group_id,product_group_name\nP1,甲\nP2,乙\nP3,丙\nP4,丁\n",
        encoding="utf-8",
    )
    await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )
    before = {t.node_key for t in await list_terms(conn, "muji")}
    assert len(before) == 4

    # 截断到只剩 1 行：将要移除 3/4 = 75%，超过 50% 阈值。
    path.write_text("product_group_id,product_group_name\nP1,甲\n", encoding="utf-8")
    graph_client = FakeGraphClient()

    with pytest.raises(SweepSafetyValveError) as excinfo:
        await run_schema_etl(
            conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
        )

    message = str(excinfo.value)
    assert "Product" in message
    assert "3" in message and "4" in message
    # 零改动：既没删，也没写。
    assert {t.node_key for t in await list_terms(conn, "muji")} == before
    assert graph_client.deleted_nodes == []
    assert graph_client.synced == []


async def test_run_schema_etl_allow_large_sweep_lets_the_run_through(tmp_path):
    """阈值是启发式，不是正确性保证。租户确实要缩减数据时必须有显式的放行
    方式，否则安全阀会把合法操作永久挡死。"""
    conn = await _confirmed_conn()
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={},
            ),
        ],
        relations=[],
    )
    path = tmp_path / "products.csv"
    path.write_text(
        "product_group_id,product_group_name\nP1,甲\nP2,乙\nP3,丙\nP4,丁\n",
        encoding="utf-8",
    )
    await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    path.write_text("product_group_id,product_group_name\nP1,甲\n", encoding="utf-8")
    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config,
        data_dir=tmp_path, allow_large_sweep=True,
    )

    assert report.entities_removed == 3
    assert {t.node_key for t in await list_terms(conn, "muji")} == {"Product:P1"}


async def test_run_schema_etl_dry_run_reports_removals_without_changing_anything(tmp_path):
    """首次启用 sweep 会清理掉历史累积的孤儿实体，规模可能不小。dry-run 让
    租户先看一眼将要删什么，再决定是否真跑。"""
    conn = await _confirmed_conn()
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={},
            ),
        ],
        relations=[],
    )
    path = tmp_path / "products.csv"
    path.write_text(
        "product_group_id,product_group_name\nP1,甲\nP2,乙\n", encoding="utf-8"
    )
    await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    path.write_text("product_group_id,product_group_name\nP1,甲\n", encoding="utf-8")
    graph_client = FakeGraphClient()
    report = await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config,
        data_dir=tmp_path, dry_run=True,
    )

    assert report.dry_run is True
    assert report.entities_removed == 1
    assert report.entities_removed_by_type == {"Product": 1}
    # 什么都没动。
    assert len(await list_terms(conn, "muji")) == 2
    assert graph_client.deleted_nodes == []
    assert graph_client.synced == []
```

**实施者注意**：`upsert_term_with_node_key` 的 `source` 参数名请核对，与 Task 1 保持一致的用法。

- [ ] **Step 2: 跑测试确认失败**

Expected: `ImportError: cannot import name 'SweepSafetyValveError'`。

- [ ] **Step 3: 扩 `ETLRunReport` 与协议**

`app/graphrag/schema_etl.py` 里，`ETLRunReport` 加四个字段（放在既有字段之后，**不要改动既有字段的名字或顺序**）：

```python
@dataclass
class ETLRunReport:
    entities_written: int = 0
    entities_skipped: int = 0
    relations_written: int = 0
    relations_skipped: int = 0
    written_by_type: dict[str, int] = field(default_factory=dict)
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    skipped_rows: list[SkippedRow] = field(default_factory=list)
    skipped_mappings: list[SkippedMapping] = field(default_factory=list)
    # 源端删除的传播（2026-08-31）。零删除时这三个字段也会出现在报告里——
    # "本次没有移除任何实体"和"根本没跑删除逻辑"必须能区分开。
    entities_removed: int = 0
    entities_removed_by_type: dict[str, int] = field(default_factory=dict)
    relations_removed: int = 0
    # dry_run=True 时，上面的删除计数是"将要删除多少"，而不是"已经删了多少"。
    dry_run: bool = False
```

`SchemaEtlGraphProtocol` 加一个方法：

```python
class SchemaEtlGraphProtocol(RelationWriterProtocol, Protocol):
    async def sync_term(self, term: Term) -> None: ...
    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None: ...
```

（保留该类原有的文档字符串，只加方法。）

新增异常，放在 `SchemaETLNotConfirmedError` 附近：

```python
class SweepSafetyValveError(Exception):
    """源端删除的清理规模超过安全阈值，整轮失败、零改动。

    一次误传的、被截断的源文件会静默清空大半个图谱，而症状要等用户提问
    答不出来才暴露。阈值和放行开关让"我确实要缩减数据"这件事必须被显式
    表达。阈值是启发式而不是正确性保证——它拦不住 49% 的误删，作用是把
    最常见的事故形态（传错文件、导出被截断）挡在门外。
    """
```

模块级常量：

```python
# 单个 term_type 的清理占比超过这个比例就触发安全阀。50% 是拍的，没有
# 数据依据——真实租户的数据波动幅度未知，可能过松也可能过紧，跑过若干
# 次真实运行后应当回头调整。
_SWEEP_SAFETY_THRESHOLD = 0.5
```

- [ ] **Step 4: 在 `run_schema_etl` 里接入 sweep 与安全阀**

签名加两个关键字参数（都有默认值，既有调用方不受影响）：

```python
async def run_schema_etl(
    *,
    conn: aiosqlite.Connection,
    graph_client: SchemaEtlGraphProtocol,
    config: SchemaETLConfig,
    data_dir: Path,
    dry_run: bool = False,
    allow_large_sweep: bool = False,
) -> ETLRunReport:
```

在既有的预检循环里，顺便把每个 mapping 的 `node_keys` 留下来。预检循环改成：

```python
    duplicates_by_term_type: dict[str, dict[str, list[int]]] = {}
    scanned_keys_by_term_type: dict[str, set[str]] = {}
    for entity_mapping in config.entities:
        if entity_mapping.term_type not in confirmed_term_type_values:
            continue
        try:
            scan = await scan_entity_node_keys(
                conn, tenant_id=config.tenant_id, mapping=entity_mapping, data_dir=data_dir,
            )
        except RowProcessingError:
            continue
        if scan.duplicate_keys:
            duplicates_by_term_type[entity_mapping.term_type] = scan.duplicate_keys
        scanned_keys_by_term_type[entity_mapping.term_type] = scan.node_keys
    if duplicates_by_term_type:
        raise DuplicateNodeKeyError(format_duplicate_key_error(duplicates_by_term_type))
```

（`confirmed_term_type_values` 是既有代码，保持不动。）

紧接着，在 `recorded_at = datetime.now()` **之前**，算 sweep 集合并判定安全阀：

```python
    # sweep 集合在这里就能算出来——预检第一遍已经持有本次源文件的全部
    # node_key。因此安全阀的判定发生在任何写入之前，"整轮零改动"是结构性
    # 的，跟 DuplicateNodeKeyError 走同一条路径，不是靠记得回滚。
    #
    # 只圈 source='etl' 的行：审核界面创建的（'review'）和管理后台手工录入
    # 的（'manual'）从来就不来自这个数据源，"源里没有"对它们不成立。
    sweep_by_term_type: dict[str, set[str]] = {}
    for term_type, scanned_keys in scanned_keys_by_term_type.items():
        existing = await list_etl_node_keys_by_term_type(conn, config.tenant_id, term_type)
        sweep_by_term_type[term_type] = existing - scanned_keys

    if not allow_large_sweep:
        for term_type, doomed in sweep_by_term_type.items():
            existing_count = len(
                await list_etl_node_keys_by_term_type(conn, config.tenant_id, term_type)
            )
            if existing_count == 0 or not doomed:
                continue
            ratio = len(doomed) / existing_count
            if ratio > _SWEEP_SAFETY_THRESHOLD:
                raise SweepSafetyValveError(
                    f"实体类型 {term_type!r} 的清理将移除 {len(doomed)} / {existing_count} 行"
                    f"（{ratio:.0%}），超过安全阈值 {_SWEEP_SAFETY_THRESHOLD:.0%}，本次未做任何改动。\n"
                    f"如果源文件确实缩减到这个规模，勾选"允许大规模清理"后重跑。"
                )
```

**实施者注意**：上面为了可读性对同一个 term_type 调了两次 `list_etl_node_keys_by_term_type`。请改成只调一次、把集合存下来复用——这是数据库查询，不该重复。

dry-run 的短路，放在安全阀之后、写入之前：

```python
    if dry_run:
        # 预演：把将要删除的规模填进报告就返回，不写入、不删除。首次启用
        # sweep 时历史累积的孤儿实体可能规模不小，让租户先看一眼。
        preview = ETLRunReport(dry_run=True)
        preview.entities_removed = sum(len(v) for v in sweep_by_term_type.values())
        preview.entities_removed_by_type = {
            term_type: len(doomed) for term_type, doomed in sweep_by_term_type.items()
        }
        return preview
```

最后，在函数 `return report` **之前**（关系写入循环之后）执行 sweep：

```python
    # sweep 的执行放在写入之后：判定必须在写入前（才能保证阀触发时零改动），
    # 但执行必须在写入后——先删后写会在中途留下实体缺失，关系写入的端点
    # 存在性守卫会大面积误判、把合法的关系行全部跳过。
    for term_type, doomed in sweep_by_term_type.items():
        report.entities_removed_by_type[term_type] = len(doomed)
        if not doomed:
            continue
        removed = await delete_terms_by_node_keys(conn, config.tenant_id, doomed)
        report.entities_removed += removed
        for node_key in doomed:
            # delete_term_node 是 DETACH DELETE，连这个节点的边和别名节点
            # 一起清掉，不会留下悬空引用。
            await graph_client.delete_term_node(
                tenant_id=config.tenant_id, node_key=node_key
            )
```

在 import 区补上：

```python
from app.graphrag.terms_store import (
    ...,
    delete_terms_by_node_keys,
    list_etl_node_keys_by_term_type,
)
```

- [ ] **Step 5: 跑测试**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `1464 passed`（1458 + 6），0 failed。

**如果既有用例开始失败**：多半是某条老用例连跑两次 ETL、第二次因 sweep 少了实体。逐条看清楚再动——不要为了让测试通过而放宽 sweep。真有夹具问题就改夹具并在报告里说明改了哪条、为什么。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/schema_etl.py tests/graphrag/test_schema_etl.py
git commit -m "feat(etl): sweep entities that vanished from the source"
```

---

## Task 3: 关系的"先写后扫"

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `app/graphrag/schema_etl.py`
- Test: `tests/graphrag/test_schema_etl.py`

**Interfaces:**
- Consumes: 边上已有的属性 `source`、`tenant_id`、`provenance`、`recorded_at`（`merge_relation` 每次 MERGE 都 `SET` 这四个，见 `neo4j_client.py:531-534`）。
- Produces: `async def delete_stale_relations_by_source(self, source: str, *, tenant_id: str, before_recorded_at: str) -> int`，加进 `SchemaEtlGraphProtocol`。

**为什么"先写后扫"是对的（用户已拍板，这里说清机制）：**

`merge_relation` 的 Cypher 末尾是 `SET r.source = $source, r.provenance = $provenance, r.recorded_at = $recorded_at`——**无条件执行**，所以重写一条已存在的边会刷新它的 `recorded_at`。`run_schema_etl` 在整轮开始时取一次 `recorded_at = datetime.now()`，把**同一个值**传给本轮每一次 `merge_relation`。

于是本轮写过的边，`recorded_at` 恰好等于本轮的时间戳；上一轮写的、本轮源里已经没有的边，`recorded_at` 严格更早。扫除条件就是 `r.recorded_at < $before`。

时间戳存的是 `"%Y-%m-%d %H:%M:%S"` 格式的**字符串**（`neo4j_client.py:545`），这个格式的字典序等于时序，可以直接用 `<` 比较。

**已知边界**：两轮 ETL 在同一秒内跑完时，上一轮的边时间戳与本轮相同，`<` 匹配不到，陈旧边会残留到下一轮。实际不可能——ETL 单轮远超一秒，且 `etl_runs` 上有"每租户同时只能有一个 running"的唯一索引。**在代码注释里写明这个边界，不要试图消除它。**

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_schema_etl.py` 的 `FakeGraphClient` 上加删除方法与记录（在 Task 2 已加的 `delete_term_node` 之外）：

```python
    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int:
        self.stale_sweeps.append((source, before_recorded_at))
        # 假客户端不真的维护边集合，返回 0；真实计数由 Neo4j 侧的实现负责。
        return 0
```

并在 `__init__` 里加 `self.stale_sweeps: list[tuple[str, str]] = []`。

在文件末尾追加：

```python
async def test_run_schema_etl_sweeps_stale_relations_after_writing_fresh_ones(tmp_path):
    """关系用"先写新边、再扫陈旧边"：任何时刻图谱都是完整的，中途失败最多
    留下新旧共存，下次重跑自愈。若改成"先全删再全写"，中途失败会留下一个
    边被删光、实体还在的图谱，而 ETL 数据量大、这个窗口很长。"""
    conn = await _confirmed_conn()
    config = _product_sku_config()
    _write_product_sku_source(tmp_path)
    graph_client = FakeGraphClient()

    await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    # 扫除发生了，且针对配置里出现过的源文件。
    assert [s for s, _ in graph_client.stale_sweeps] == ["products.csv"]
    # 扫除的时间界线就是本轮的写入时间——本轮写的边一律不会被扫掉。
    assert graph_client.stale_sweeps[0][1]


async def test_run_schema_etl_sweeps_each_source_file_once_not_once_per_mapping(tmp_path):
    """多条关系映射共享同一个源文件时（demo 配置里五条关系全部来自
    soft_drink_sales.xlsx），扫除必须按源文件去重，不能每个映射扫一遍。"""
    conn = await _confirmed_conn()
    config = _two_relations_same_source_config()
    _write_product_sku_source(tmp_path)
    graph_client = FakeGraphClient()

    await run_schema_etl(
        conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
    )

    assert [s for s, _ in graph_client.stale_sweeps] == ["products.csv"]
    # 两种关系都写进去了，没有互相抹掉。
    assert {r for _, _, r in graph_client.merged} == {"HAS_SKU", "HAS_VARIANT_VALUE"}
```

**实施者注意**：`_product_sku_config()`、`_write_product_sku_source()`、`_two_relations_same_source_config()` 这三个辅助函数**本计划没有给出实现**，因为它们要贴合该测试文件既有的夹具风格（`_confirmed_conn()` 建的租户是 `"muji"`，术语类型是 `Product`/`SKU`/`VariantValue`，关系类型是 `HAS_SKU`/`HAS_VARIANT_VALUE`）。请照该文件里已有的同类用例写这三个辅助函数，或者直接内联构造 `SchemaETLConfig`——怎么写都行，但**必须真的构造出"两条关系映射共享同一个 source_file"的配置**，那是第二条用例唯一要验的东西。

- [ ] **Step 2: 跑测试确认失败**

Expected: `AttributeError` 或断言失败（`stale_sweeps` 为空——还没有人调用扫除）。

- [ ] **Step 3: 在 `neo4j_client.py` 实现扫除**

在 `delete_relations_by_source`（约 :549）之后加：

```python
_DELETE_STALE_RELATIONS_QUERY = """
MATCH (a:Term {tenant_id: $tenant_id})-[r]->(b:Term {tenant_id: $tenant_id})
WHERE r.tenant_id = $tenant_id
  AND r.source = $source
  AND r.provenance = $provenance
  AND r.recorded_at < $before
DELETE r
RETURN count(r) AS removed
"""
# 只删本次运行没有重写过的边：merge_relation 每次 MERGE 都无条件
# SET r.recorded_at，所以本轮写过的边时间戳恰好等于本轮的值，严格早于
# 它的就是"上一轮写过、这一轮源里已经没有"的陈旧边。
#
# recorded_at 存的是 "%Y-%m-%d %H:%M:%S" 字符串，这个格式的字典序等于
# 时序，可以直接用 < 比较。
#
# 已知边界：两轮 ETL 在同一秒内跑完时，上一轮的时间戳与本轮相同、匹配
# 不到，陈旧边会残留到下一轮。实际不可能——单轮 ETL 远超一秒，且
# etl_runs 上有"每租户同时只能有一个 running"的唯一索引。
#
# provenance 也进过滤条件：同名 source 的边如果是抽取管道写的
# （AUTO_MERGED / HUMAN_APPROVED），不该被 ETL 的清理波及。
```

方法：

```python
    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int:
        """删除某个源文件下、本次 ETL 运行没有重写过的关系边，返回删除条数。

        与 delete_relations_by_source（全删）的区别是它只删陈旧的那些——
        配合"先写新边、再扫陈旧边"的顺序，图谱在任何时刻都是完整的。
        """
        async with self._driver.session() as session:
            result = await session.run(
                _DELETE_STALE_RELATIONS_QUERY,
                {
                    "source": source,
                    "tenant_id": tenant_id,
                    "provenance": provenance.ETL,
                    "before": before_recorded_at,
                },
            )
            record = await result.single()
            return record["removed"] if record else 0
```

**实施者注意**：`provenance` 模块在 `neo4j_client.py` 里可能尚未导入，需要 `from app.graphrag import provenance`；也可以把 `provenance` 作为方法参数由调用方传入。两种都行，选一种并在报告里说明。另外 `DELETE r ... RETURN count(r)` 的写法在某些 Neo4j 版本上行为不同，请实际验证返回值语义；若不可靠，改成先 `WITH collect(r) AS rs` 再统计，或分两次查询。**返回值必须是真实删除条数，不能是猜的。**

在 `SchemaEtlGraphProtocol`（`schema_etl.py`）里加上：

```python
    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int: ...
```

- [ ] **Step 4: 在 `run_schema_etl` 里调用扫除**

在关系写入循环**之后**、实体 sweep 之前（或之后，顺序无关）加：

```python
    # 关系用"先写后扫"：本轮该写的边都已 MERGE 完（时间戳被刷新成本轮的
    # 值），现在删掉同源下时间戳更早的——那些就是上一轮写过、这一轮源里
    # 已经没有的边。
    #
    # 按源文件去重：多条关系映射常常共享同一个源文件（demo 配置里五条关系
    # 全部来自 soft_drink_sales.xlsx），逐映射扫一遍是重复劳动。
    recorded_at_text = recorded_at.strftime("%Y-%m-%d %H:%M:%S")
    for source_file in dict.fromkeys(m.source_file for m in config.relations):
        report.relations_removed += await graph_client.delete_stale_relations_by_source(
            source_file, tenant_id=config.tenant_id, before_recorded_at=recorded_at_text,
        )
```

**注意**：`recorded_at` 传给 `merge_relation` 时由 `neo4j_client` 内部做 `strftime`；这里要自己格式化成同样的字符串。两处格式必须**完全一致**，否则比较会错。`dict.fromkeys` 用来按源文件去重并保持顺序。

dry-run 分支已经在关系写入之前 return 了，所以不需要额外处理。

- [ ] **Step 5: 跑全量**

Expected: `1466 passed`（1464 + 2），0 failed。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/neo4j_client.py app/graphrag/schema_etl.py tests/graphrag/test_schema_etl.py
git commit -m "feat(etl): sweep relation edges the source no longer produces"
```

---

## Task 4: 把两个开关接到管理后台

**Files:**
- Modify: `app/api/admin_schema_etl_routes.py`
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`
- Test: `tests/api/test_admin_schema_etl_routes.py`

**Interfaces:**
- Consumes: `run_schema_etl(..., dry_run: bool = False, allow_large_sweep: bool = False)`（Task 2）
- Produces: 跑批入口 `POST /api/admin/{tenant_id}/schema-etl/runs` 接受两个可选表单字段 `dry_run`、`allow_large_sweep`。

**关键：这个路由是 `multipart/form-data`，不是 JSON。** `start_schema_etl_run` 的参数里已经有 `config: UploadFile` 和 `data_files: list[UploadFile]`，新字段必须用 `Form(...)` 声明，不能放进 `BaseModel` 请求体。

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_schema_etl_routes.py` 末尾追加（**请先读该文件已有用例，沿用它的客户端夹具与 `dependency_overrides` 方式，下面的代码是形状示意，参数细节按该文件实际风格调整**）：

```python
async def test_start_run_passes_dry_run_and_allow_large_sweep_through(...):
    """两个开关必须真的透传到 run_schema_etl——只在路由上接收却不往下传，
    是这种"加开关"改动最典型的静默失效。"""
    captured: dict[str, object] = {}

    async def fake_run_schema_etl(*, conn, graph_client, config, data_dir,
                                  dry_run=False, allow_large_sweep=False):
        captured["dry_run"] = dry_run
        captured["allow_large_sweep"] = allow_large_sweep
        return ETLRunReport()

    # 打桩 run_schema_etl，提交带 dry_run=true & allow_large_sweep=true 的表单
    ...

    assert captured == {"dry_run": True, "allow_large_sweep": True}


async def test_start_run_defaults_both_switches_to_false(...):
    """不传两个字段时必须默认关闭——安全阀默认生效，dry-run 默认不生效。
    默认值搞反会让安全阀形同虚设。"""
    ...
    assert captured == {"dry_run": False, "allow_large_sweep": False}
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 路由接收并透传**

`app/api/admin_schema_etl_routes.py`：import 区补 `Form`（`from fastapi import ..., Form`）。

`start_schema_etl_run` 加两个参数（放在 `data_files` 之后、依赖注入参数之前）：

```python
    dry_run: bool = Form(False),
    allow_large_sweep: bool = Form(False),
```

把它们透传进 `background_tasks.add_task(...)` 对 `_run_schema_etl_job` 的调用；`_run_schema_etl_job` 也相应加这两个参数，并传给 `run_schema_etl`：

```python
        report = await run_schema_etl(
            conn=conn, graph_client=graph_client, config=config, data_dir=data_dir,
            dry_run=dry_run, allow_large_sweep=allow_large_sweep,
        )
```

**不要改 `_run_schema_etl_job` 的 `except Exception` 兜底**——`SweepSafetyValveError` 要走它落到 `status='failed'` 并保留完整消息，跟 `DuplicateNodeKeyError` 一样。前端已经渲染 `失败：{selectedRun.error}`（且带 `whitespace-pre-wrap`）。

- [ ] **Step 4: 前端加两个勾选框**

`frontend/src/admin/SchemaEtlPage.tsx` 的上传表单里，在提交按钮附近加两个 checkbox（`name="dry_run"` / `name="allow_large_sweep"`），并在 `handleUpload`（约 :212）里读取它们、追加进 `FormData`：

```ts
      const dryRunInput = form.elements.namedItem('dry_run') as HTMLInputElement | null
      const allowLargeSweepInput = form.elements.namedItem('allow_large_sweep') as HTMLInputElement | null
      formData.append('dry_run', String(dryRunInput?.checked ?? false))
      formData.append('allow_large_sweep', String(allowLargeSweepInput?.checked ?? false))
```

两个勾选框要有可见的 `<label>`（不能只靠 placeholder），并各配一句说明文字：
- **预演（不写入）**：只报告将要移除多少实体，不做任何写入或删除。
- **允许大规模清理**：本次移除比例超过安全阈值时也继续。**默认不勾**。

样式跟随该页面既有表单控件，不要引入新的设计元素。

- [ ] **Step 5: 验证**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
cd frontend && npx tsc --noEmit && echo "typecheck ok"
```

Expected: `1468 passed`（1466 + 2），0 failed；typecheck 无错误。

- [ ] **Step 6: 提交**

```bash
git add app/api/admin_schema_etl_routes.py frontend/src/admin/SchemaEtlPage.tsx tests/api/test_admin_schema_etl_routes.py
git commit -m "feat(admin): expose dry-run and large-sweep override on ETL runs"
```

---

## Self-Review

**1. Spec coverage**

| spec 章节 | 对应任务 |
|---|---|
| 设计 A：实体 mark-and-sweep，按 term_type 圈定 | Task 1（存储层）+ Task 2（接入） |
| 人工创建的实体不被 sweep 波及 | Task 1 的 `list_etl_node_keys_by_term_type` + Task 2 的对应用例 |
| 设计 B：关系按源删除 | Task 3（改为"先写后扫"，见修正一） |
| 设计 C：与人工编辑层的优先级 | **无任务**——spec 明说"Spec 3 未落地时，这一节不适用"。Spec 3 尚未实现。 |
| 设计 D：报告字段 | Task 2 的 `ETLRunReport` 四个新字段 |
| 设计 D：安全阀 | Task 2 |
| 迁移：dry-run 模式 | Task 2（引擎）+ Task 4（后台入口） |
| 测试策略的六条 | 源端删除→T2；人工不被波及→T2；关系全量替换→T3；多映射共享源→T3；安全阀零改动→T2；与编辑层优先级→不适用（Spec 3 未落地） |
| 未决风险：删除窗口 | 用户拍板"先写后扫"，Task 3 |
| 未决风险：孤儿边 | 已解决，见修正二；Task 2 有断言钉住 |
| 未决风险：阈值无依据 | Task 2 的 `_SWEEP_SAFETY_THRESHOLD` 常量 + 注释写明"拍的、应回头调整" |
| 未决风险：`terms.source` 承担新职责 | Task 1 的两条用例把"只扫 etl 行"钉进测试 |
| 未决风险：源文件本身消失 | **不在范围内**，spec 明说属配置生命周期管理 |

**2. Placeholder scan**：三处刻意留给实施者的地方，都写明了理由与验收标准——Task 1 的 `upsert_term_with_node_key` 的 `source` 参数名、Task 3 的三个测试辅助函数（必须贴合既有夹具风格）、Task 3 的 `DELETE r ... RETURN count(r)` 返回值语义（必须实际验证，不能猜）。Task 4 Step 1 的测试是形状示意，明确要求先读既有用例再照其风格写。其余步骤均给了可直接粘贴的完整代码。

**3. Type consistency**：`list_etl_node_keys_by_term_type` 与 `delete_terms_by_node_keys` 在 Task 1 定义、Task 2 消费，签名一致；`KeyScanResult.node_keys: set[str]` 在 Task 1 加、Task 2 按 `scan.node_keys` 消费；`delete_stale_relations_by_source(source, *, tenant_id, before_recorded_at) -> int` 在 Task 3 的 neo4j 实现、协议声明、FakeGraphClient、调用点四处签名一致；`ETLRunReport` 的四个新字段在 Task 2 定义、Task 2/3 写入、Task 4 透传。

**4. 测试计数链**：1454（基线）→ 1458（T1，+4）→ 1464（T2，+6）→ 1466（T3，+2）→ 1468（T4，+2）。
