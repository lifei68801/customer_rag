# 人工编辑独立成层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把人工编辑从 `terms` 表里剥出来，写进独立的 `term_edits` 表，让 ETL 重跑不再静默抹掉人工修正。

**Architecture:** 新增 `term_edits` 表（每个 `(tenant_id, node_key, field)` 一行，保存当前编辑状态）。读路径统一走"管道产出 + 编辑"的合并视图。写路径分化：ETL 只写 `terms`，一切人工路径只写 `term_edits`。Neo4j 是合并结果的投影。

**Tech Stack:** Python 3.12、aiosqlite、Neo4j、FastAPI、pytest + anyio。

**Spec:** [docs/superpowers/specs/2026-08-30-manual-edits-layer-design.md](../specs/2026-08-30-manual-edits-layer-design.md)

## Global Constraints

- **ETL 写入路径永不写 `term_edits`；人工编辑路径永不写 `terms`。** 这条是本设计的全部价值所在，任何一处违反都会让"重跑 ETL 不伤人工修正"的保证静默失效。
- 合并策略固定为 **Apply User Edits**（编辑对被编辑字段优先，未编辑字段正常接受 ETL 更新），不引入时间戳比较。
- 编辑是**字段级**的，不是整行级。
- **人工删除不可被 ETL 恢复。**
- Neo4j 是合并结果的投影，不是 `terms` 的投影。
- `node_key` 是编辑与管道产出之间唯一的对应键；不引入任何会改写 `node_key` 的路径（ADR-0003）。
- 测试基线 **1471 passed**。每个任务结束时全量必须 `0 failed`。
- **pytest 打完 summary 会卡在 teardown**（aiosqlite 工作线程非 daemon）。跑测试一律用：
  `PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1; grep -E "passed|failed" /tmp/o.txt | tail -2`
  退出码 124 是预期的，不是失败。
- 前端**没有测试框架**（无 vitest/jest），用 `cd frontend && npx tsc --noEmit` 验证。
- 注释和文档字符串用中文，跟现有代码一致。
- f-string 里不要内嵌 ASCII 双引号（会破坏定界符造成 SyntaxError）。

## 用户已拍板的范围

`duplicate_review_queue` 的合并术语（`merge_terms`）**包含在本次范围内**（Task 5）。spec 建议过可以单独成任务，但把它排除会让 Global Constraints 的第一条出现一处静默破例——批准的合并仍会被下一次 ETL 重跑抹掉。

## 三处对 spec 的修正（写计划时实读代码得出，实施时以本节为准）

**修正一 · spec 写路径表里的两行其实是同一个端点。**
spec 的表把"`admin_terms_routes.py` 的 PUT / DELETE"和"`GraphReviewsPage` 审核界面现场创建实体"列成两行。实读后：`create_term` 全库**只有一个生产调用点**——`app/api/admin_terms_routes.py:151`（`create_new_term`，POST 路由），而审核界面的内联创建走的就是它（`terms_store.py:563-568` 的文档字符串明确说明）。所以是**一个端点**，Task 4 一并处理。

spec 的表还**漏了 POST**（只列了 PUT/DELETE）。按 Global Constraints 第一条，POST 必须写 `__created__` 到编辑层。这是 spec 的疏漏，不是设计意图，本计划补上。

**修正二 · `merge_terms` 改到编辑层之后会变简单，不是变复杂。**
spec 的未决风险说这一处"复杂度可能不低于本设计的其余部分"。实读后判断相反：`merge_terms` 现在那套"墓碑化 merged → 追加别名到 keep → 失败时补偿恢复 → 补偿也失败时记 ERROR 日志"的机制（`terms_store.py:775-840` 一带），**唯一存在理由是绕开 `_check_name_conflict`**——不先把 merged 那条墓碑化，把它的 `standard_name` 追加成 keep 的别名就会撞上冲突检查。

编辑层没有这道检查（编辑写的是 `term_edits`，不走 `update_term`）。于是合并退化成**两次独立、幂等的编辑写入**：merged 那条写 `__deleted__`，keep 那条写 `aliases`。没有顺序依赖，没有中间态，补偿回滚整个不需要。

**修正三 · 存量编辑无法回填这一点必须在上线前告知，不是实施任务能解决的。**
`terms` 表没有记录哪些字段是人工改过的（`source` 列只标记**创建**渠道）。本设计上线前已存在的人工修正，在下一次 ETL 重跑时仍会被覆盖一次。本计划不试图猜测哪些行被人工改过——那只会制造错误的编辑记录。Task 7 的报告里要写明这一点。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `app/graphrag/term_edits_store.py`（新建） | `term_edits` 表的建表与增删查。不认识合并语义。 |
| `app/graphrag/term_merge.py`（新建） | 纯函数：把 `list[Term]` 和编辑字典合并成 `list[Term]`。不碰数据库，因此可以被穷举地单测。 |
| `app/graphrag/terms_store.py`（改） | 新增 `list_terms_merged` / `get_term_merged_by_node_key`——查两张表 + 调纯函数。`merge_terms` 改写编辑层。 |
| `app/graphrag/ontology_store.py`（改） | 建表清单加 `ensure_term_edits_schema`。 |
| `app/api/admin_terms_routes.py`（改） | POST/PUT/DELETE 三个写入端点改写编辑层；读端点改走合并视图。 |
| 其余 13 处读路径（改） | 改走 `list_terms_merged`。 |
| `app/graphrag/schema_etl.py`（改） | `sync_term` 的入参改从合并视图取。 |
| `tests/graphrag/test_term_edits_store.py`（新建） | 存储层单测。 |
| `tests/graphrag/test_term_merge.py`（新建） | 合并语义的穷举单测。 |
| `tests/graphrag/test_manual_edits_integration.py`（新建） | 端到端核心保证。 |

---

## Task 1: `term_edits` 表与存储层

**Files:**
- Create: `app/graphrag/term_edits_store.py`
- Modify: `app/graphrag/ontology_store.py`（建表清单）
- Test: `tests/graphrag/test_term_edits_store.py`（新建）

**Interfaces:**
- Produces:
  - `async def ensure_term_edits_schema(conn) -> None`
  - `async def upsert_term_edit(conn, *, tenant_id: str, node_key: str, field: str, value: object, edited_by: str) -> None`
  - `async def delete_term_edit(conn, *, tenant_id: str, node_key: str, field: str) -> None`
  - `async def list_term_edits(conn, tenant_id: str) -> dict[str, dict[str, object]]` —— `{node_key: {field: value}}`
  - `async def list_term_edits_for_node_key(conn, tenant_id: str, node_key: str) -> dict[str, object]`
  - 模块常量 `FIELD_DELETED = "__deleted__"`、`FIELD_CREATED = "__created__"`、`EXTRA_PROPERTY_PREFIX = "extra_properties."`

**表结构（spec 给定，逐字采用）：**

```sql
CREATE TABLE IF NOT EXISTS term_edits (
    tenant_id     TEXT NOT NULL,
    node_key      TEXT NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,
    edited_at     TEXT NOT NULL,
    edited_by     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_key, field)
);
```

`value` 存 JSON 文本（`json.dumps(..., ensure_ascii=False)`）；`__deleted__` 的 `value` 存 SQL `NULL`。

- [ ] **Step 1: 写失败的测试 `tests/graphrag/test_term_edits_store.py`**

```python
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.term_edits_store import (
    FIELD_CREATED,
    FIELD_DELETED,
    delete_term_edit,
    ensure_term_edits_schema,
    list_term_edits,
    list_term_edits_for_node_key,
    upsert_term_edit,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_term_edits_schema(conn)
    return conn


async def test_upsert_and_read_back_a_field_edit():
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="人工改过的名字", edited_by="alice",
    )

    assert await list_term_edits(conn, "t1") == {
        "产品:A": {"standard_name": "人工改过的名字"}
    }


async def test_upsert_twice_on_the_same_field_keeps_only_the_last_value():
    """term_edits 保存的是当前编辑状态，不是 append-only 日志——同一个
    (node_key, field) 改两次只剩最后一次。见 spec 的非目标。"""
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="第一次", edited_by="alice",
    )
    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="第二次", edited_by="bob",
    )

    assert await list_term_edits(conn, "t1") == {"产品:A": {"standard_name": "第二次"}}


async def test_edits_round_trip_lists_and_dicts_without_losing_types():
    """value 是 JSON 文本。aliases 是列表、extra_properties.<name> 可能是
    数值——存进去再读出来必须还是原来的类型，不能变成字符串。"""
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="aliases",
        value=["别名一", "别名二"], edited_by="alice",
    )
    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="extra_properties.revenue",
        value=1234.5, edited_by="alice",
    )

    edits = await list_term_edits_for_node_key(conn, "t1", "产品:A")

    assert edits["aliases"] == ["别名一", "别名二"]
    assert edits["extra_properties.revenue"] == 1234.5


async def test_deleted_marker_is_stored_with_a_null_value():
    """__deleted__ 的 value 是 SQL NULL。读回来时这个字段必须存在于字典里
    （值为 None）——"有删除标记但值是 None"和"根本没有删除标记"是两件事，
    合并视图靠前者把实体整个排除掉。"""
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field=FIELD_DELETED,
        value=None, edited_by="alice",
    )

    edits = await list_term_edits_for_node_key(conn, "t1", "产品:A")

    assert FIELD_DELETED in edits
    assert edits[FIELD_DELETED] is None


async def test_created_marker_holds_the_full_field_set():
    conn = await _conn()

    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:NEW", field=FIELD_CREATED,
        value={"standard_name": "新建", "term_type": "产品", "aliases": []},
        edited_by="alice",
    )

    edits = await list_term_edits_for_node_key(conn, "t1", "产品:NEW")

    assert edits[FIELD_CREATED]["standard_name"] == "新建"


async def test_delete_term_edit_removes_only_that_field():
    conn = await _conn()
    for field, value in (("standard_name", "改名"), ("aliases", ["x"])):
        await upsert_term_edit(
            conn, tenant_id="t1", node_key="产品:A", field=field,
            value=value, edited_by="alice",
        )

    await delete_term_edit(conn, tenant_id="t1", node_key="产品:A", field="aliases")

    assert await list_term_edits_for_node_key(conn, "t1", "产品:A") == {
        "standard_name": "改名"
    }


async def test_edits_are_scoped_to_tenant():
    conn = await _conn()
    await upsert_term_edit(
        conn, tenant_id="t1", node_key="产品:A", field="standard_name",
        value="t1 的值", edited_by="alice",
    )
    await upsert_term_edit(
        conn, tenant_id="t2", node_key="产品:A", field="standard_name",
        value="t2 的值", edited_by="alice",
    )

    assert await list_term_edits(conn, "t1") == {"产品:A": {"standard_name": "t1 的值"}}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest tests/graphrag/test_term_edits_store.py -q > /tmp/o.txt 2>&1
grep -E "passed|failed|Error" /tmp/o.txt | tail -3
```

Expected: `ModuleNotFoundError: No module named 'app.graphrag.term_edits_store'`。

- [ ] **Step 3: 实现 `app/graphrag/term_edits_store.py`**

```python
"""人工编辑层的存储：term_edits 表的建表与增删查。

这张表跟 terms 是物理分离的两批行——terms 由管道（ETL/抽取）维护，
term_edits 只由人工路径写入。读路径把两者合并（见 term_merge.py），
于是"重跑 ETL 不伤人工修正"成为结构性保证，而不是靠约定。
见 docs/superpowers/specs/2026-08-30-manual-edits-layer-design.md。

这一层只管存取，不认识合并语义——合并是 term_merge.py 的事。
"""

from __future__ import annotations

import json
from datetime import datetime

import aiosqlite

# 删除标记。value 存 SQL NULL；合并视图遇到它就把该实体整个排除。
FIELD_DELETED = "__deleted__"
# 编辑层创建标记。value 是创建时的完整字段对象。
FIELD_CREATED = "__created__"
# 属性字段编辑的 field 前缀，例如 "extra_properties.revenue"。
EXTRA_PROPERTY_PREFIX = "extra_properties."

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS term_edits (
    tenant_id     TEXT NOT NULL,
    node_key      TEXT NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,
    edited_at     TEXT NOT NULL,
    edited_by     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_key, field)
);
CREATE INDEX IF NOT EXISTS idx_term_edits_tenant ON term_edits (tenant_id);
"""


async def ensure_term_edits_schema(conn: aiosqlite.Connection) -> None:
    """建表，幂等。由 ontology_store.open_ontology_store_conn 统一调用——
    那是本项目唯一的建表入口（2026-08-30 起）。"""
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def upsert_term_edit(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    field: str,
    value: object,
    edited_by: str,
) -> None:
    """写入或覆盖一条字段级编辑。

    term_edits 保存的是**当前编辑状态**，不是 append-only 日志：同一个
    (tenant_id, node_key, field) 改两次只剩最后一次。需要审计流水时要
    另行设计，见 spec 的非目标。

    field == FIELD_DELETED 时 value 传 None，落库为 SQL NULL。其余字段的
    value 序列化成 JSON 文本——aliases 是列表、extra_properties.<name>
    可能是数值，不走 JSON 会在读回来时全变成字符串。
    """
    stored = None if field == FIELD_DELETED else json.dumps(value, ensure_ascii=False)
    await conn.execute(
        "INSERT INTO term_edits (tenant_id, node_key, field, value, edited_at, edited_by) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, node_key, field) DO UPDATE SET "
        "value = excluded.value, edited_at = excluded.edited_at, edited_by = excluded.edited_by",
        (tenant_id, node_key, field, stored, datetime.now().isoformat(), edited_by),
    )
    await conn.commit()


async def delete_term_edit(
    conn: aiosqlite.Connection, *, tenant_id: str, node_key: str, field: str
) -> None:
    """撤掉某一个字段的编辑——该字段之后重新跟随管道产出的值。"""
    await conn.execute(
        "DELETE FROM term_edits WHERE tenant_id = ? AND node_key = ? AND field = ?",
        (tenant_id, node_key, field),
    )
    await conn.commit()


def _row_to_value(field: str, raw: str | None) -> object:
    if field == FIELD_DELETED:
        return None
    return json.loads(raw) if raw is not None else None


async def list_term_edits(
    conn: aiosqlite.Connection, tenant_id: str
) -> dict[str, dict[str, object]]:
    """该租户的全部编辑，按 {node_key: {field: value}} 组织。

    合并视图一次性取全量：term_edits 通常远小于 terms（只有被人工碰过的
    行），逐个 node_key 查会在 list_terms_merged 里退化成 N+1。
    """
    cursor = await conn.execute(
        "SELECT node_key, field, value FROM term_edits WHERE tenant_id = ?",
        (tenant_id,),
    )
    edits: dict[str, dict[str, object]] = {}
    for node_key, field, raw in await cursor.fetchall():
        edits.setdefault(node_key, {})[field] = _row_to_value(field, raw)
    return edits


async def list_term_edits_for_node_key(
    conn: aiosqlite.Connection, tenant_id: str, node_key: str
) -> dict[str, object]:
    """单个实体的全部编辑，供按 node_key 取单条的读路径用。"""
    cursor = await conn.execute(
        "SELECT field, value FROM term_edits WHERE tenant_id = ? AND node_key = ?",
        (tenant_id, node_key),
    )
    return {field: _row_to_value(field, raw) for field, raw in await cursor.fetchall()}
```

- [ ] **Step 4: 接进唯一的建表入口**

`app/graphrag/ontology_store.py`：import 区加

```python
from app.graphrag.term_edits_store import ensure_term_edits_schema
```

在 `open_ontology_store_conn` 的建表序列里（`ensure_terms_schema` 之后、`ensure_ontology_schema` 之前）加一行：

```python
        await ensure_term_edits_schema(conn)
```

- [ ] **Step 5: 跑测试确认通过，再跑全量**

Expected: 新文件 `7 passed`；全量 `1478 passed`（1471 + 7），0 failed。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/term_edits_store.py app/graphrag/ontology_store.py tests/graphrag/test_term_edits_store.py
git commit -m "feat(graphrag): add a term_edits table for manual edits"
```

---

## Task 2: 合并视图

**Files:**
- Create: `app/graphrag/term_merge.py`
- Modify: `app/graphrag/terms_store.py`（新增两个合并查询）
- Test: `tests/graphrag/test_term_merge.py`（新建）

**Interfaces:**
- Consumes: `list_term_edits(conn, tenant_id) -> dict[str, dict[str, object]]`、`list_term_edits_for_node_key(...)`、`FIELD_DELETED`、`FIELD_CREATED`、`EXTRA_PROPERTY_PREFIX`（Task 1）；`Term`（`app/graphrag/ontology.py`，字段：`tenant_id`、`node_key`、`standard_name`、`aliases: list[str]`、`term_type`、`extra_properties: dict`、`source: str = "unknown"`，`frozen=True`）
- Produces:
  - `def apply_edits(terms: list[Term], edits: dict[str, dict[str, object]], *, tenant_id: str) -> list[Term]` —— 纯函数
  - `async def list_terms_merged(conn, tenant_id, *, limit=None, offset=0, source=None) -> list[Term]`
  - `async def get_term_merged_by_node_key(conn, tenant_id, node_key) -> Term`

**为什么合并逻辑做成纯函数：** 合并语义有六七种组合（字段编辑 / 删除 / 创建 / 创建后 ETL 又产出 / 属性字段单独编辑 / 编辑指向不存在的实体），把它和数据库查询分开，才能穷举地单测。`list_terms_merged` 只负责查两张表再调它。

**合并语义（spec 给定，逐条实现）：**

| 情形 | 结果 |
|---|---|
| `terms` 有行，无编辑 | 原样返回 |
| `terms` 有行，某字段有编辑 | 该字段取编辑值，**其余字段仍取 `terms` 的值** |
| `terms` 有行，有 `__deleted__` | **整个排除**——人工删除不可被 ETL 恢复 |
| `terms` 无行，有 `__created__` | 由 `__created__` 合成一个 Term |
| `terms` 有行，且有 `__created__` | **`terms` 的行接管存在性**，`__created__` 里的每个字段降级为普通字段级编辑 |
| `terms` 无行，只有普通字段编辑（无 `__created__`） | 忽略——编辑挂在一个不存在的实体上，不凭空造实体 |

**最后两行没有外部先例，是本设计自己的判断，必须用测试钉死。**

- [ ] **Step 1: 写失败的测试 `tests/graphrag/test_term_merge.py`**

```python
from __future__ import annotations

import pytest

from app.graphrag.ontology import Term
from app.graphrag.term_edits_store import FIELD_CREATED, FIELD_DELETED
from app.graphrag.term_merge import apply_edits


def _term(node_key: str, standard_name: str, **kwargs) -> Term:
    return Term(
        tenant_id="t1",
        node_key=node_key,
        standard_name=standard_name,
        aliases=kwargs.get("aliases", []),
        term_type=kwargs.get("term_type", "产品"),
        extra_properties=kwargs.get("extra_properties", {}),
        source=kwargs.get("source", "etl"),
    )


def test_no_edits_returns_terms_unchanged():
    terms = [_term("产品:A", "甲"), _term("产品:B", "乙")]

    assert apply_edits(terms, {}, tenant_id="t1") == terms


def test_field_edit_wins_for_that_field_only():
    """这是本设计的核心保证：人工改过的字段永远优先，未编辑的字段正常
    接受管道更新。整行覆盖会让 ETL 对未编辑字段的更新一并失效。"""
    terms = [_term("产品:A", "管道产出的名字", extra_properties={"revenue": 100})]
    edits = {"产品:A": {"standard_name": "人工改过的名字"}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].standard_name == "人工改过的名字"
    assert merged[0].extra_properties == {"revenue": 100}


def test_extra_property_edit_is_field_level_within_extra_properties():
    """extra_properties.<name> 只覆盖那一个属性，同一个字典里其余属性
    仍然跟随管道。"""
    terms = [_term("产品:A", "甲", extra_properties={"revenue": 100, "cost": 60})]
    edits = {"产品:A": {"extra_properties.revenue": 999}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].extra_properties == {"revenue": 999, "cost": 60}


def test_aliases_and_term_type_edits_apply():
    terms = [_term("产品:A", "甲", aliases=["旧别名"], term_type="产品")]
    edits = {"产品:A": {"aliases": ["新别名一", "新别名二"], "term_type": "类目"}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert merged[0].aliases == ["新别名一", "新别名二"]
    assert merged[0].term_type == "类目"


def test_deleted_edit_excludes_the_term_entirely():
    """人工删除不可被 ETL 恢复——terms 表里的行还在（ETL 还在维护它），
    但对所有读路径不可见。"""
    terms = [_term("产品:A", "甲"), _term("产品:B", "乙")]
    edits = {"产品:A": {FIELD_DELETED: None}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert [t.node_key for t in merged] == ["产品:B"]


def test_deleted_wins_even_when_other_field_edits_exist():
    terms = [_term("产品:A", "甲")]
    edits = {"产品:A": {FIELD_DELETED: None, "standard_name": "改过的名字"}}

    assert apply_edits(terms, edits, tenant_id="t1") == []


def test_created_edit_synthesizes_a_term_that_terms_table_lacks():
    """审核员批准一条关系时可能需要当场创建一个尚不存在的端点实体。
    这条路径是抽取管道能闭环的必要条件。"""
    edits = {
        "产品:NEW": {
            FIELD_CREATED: {
                "standard_name": "人工新建",
                "term_type": "产品",
                "aliases": ["别名"],
                "extra_properties": {"note": "x"},
            }
        }
    }

    merged = apply_edits([], edits, tenant_id="t1")

    assert len(merged) == 1
    assert merged[0].node_key == "产品:NEW"
    assert merged[0].standard_name == "人工新建"
    assert merged[0].aliases == ["别名"]
    assert merged[0].extra_properties == {"note": "x"}


def test_created_then_etl_produces_the_same_node_key(
):
    """**这条没有外部先例，是本设计自己的判断。**

    __created__ 的语义是"这个实体在数据源里不存在，我先建一个"。一旦
    数据源真的产出了它，那个前提就不再成立——数据源是更权威的来源，
    它的行接管该实体的存在性。但当初手工填的那些字段值仍然是人的判断，
    应当继续按字段级编辑优先。

    所以：__created__ 覆盖过的字段保持人工值，未覆盖的字段取 ETL 值。
    """
    terms = [
        _term(
            "产品:NEW", "ETL 产出的名字",
            aliases=["ETL 别名"],
            extra_properties={"revenue": 500, "cost": 300},
        )
    ]
    edits = {
        "产品:NEW": {
            FIELD_CREATED: {
                "standard_name": "人工新建",
                "term_type": "产品",
                "extra_properties": {"revenue": 999},
            }
        }
    }

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert len(merged) == 1
    # __created__ 覆盖过的字段：保持人工值。
    assert merged[0].standard_name == "人工新建"
    assert merged[0].extra_properties["revenue"] == 999
    # __created__ 没覆盖的字段：取 ETL 值。
    assert merged[0].aliases == ["ETL 别名"]
    assert merged[0].extra_properties["cost"] == 300


def test_orphan_field_edits_without_created_are_ignored():
    """编辑挂在一个 terms 表里不存在、也没有 __created__ 的 node_key 上
    ——不凭空造实体。这种孤儿编辑通常来自实体被 ETL 的 sweep 清理掉之后
    （见源端删除传播那份设计：sweep 只删 terms 行、不删 term_edits 行，
    源里若再出现同 node_key，编辑重新生效）。"""
    edits = {"产品:GONE": {"standard_name": "改过的名字"}}

    assert apply_edits([], edits, tenant_id="t1") == []


def test_edits_for_other_tenants_node_keys_do_not_leak():
    """apply_edits 拿到的 terms 和 edits 应当已经是同一个租户的。这条用例
    钉的是函数不会因为 edits 里有多余的 key 而凭空产出 Term。"""
    terms = [_term("产品:A", "甲")]
    edits = {"产品:A": {"standard_name": "改过"}, "产品:别的租户的": {"standard_name": "x"}}

    merged = apply_edits(terms, edits, tenant_id="t1")

    assert [t.node_key for t in merged] == ["产品:A"]
```

- [ ] **Step 2: 跑测试确认失败**

Expected: `ModuleNotFoundError: No module named 'app.graphrag.term_merge'`。

- [ ] **Step 3: 实现 `app/graphrag/term_merge.py`**

```python
"""管道产出与人工编辑的合并（Apply User Edits）。

合并策略：**人工编辑对被编辑的字段永远优先，未被编辑的字段正常接受
管道更新**。不采用按时间戳比较的策略——那要求背书数据带时间戳列，而
源文件是客户上传的 xlsx，不保证有这一列。
见 docs/superpowers/specs/2026-08-30-manual-edits-layer-design.md。

刻意做成不碰数据库的纯函数：合并语义有六七种组合（字段编辑 / 删除 /
创建 / 创建后管道又产出同 node_key / 属性字段单独编辑 / 孤儿编辑），
只有跟查询分开才能穷举地单测。
"""

from __future__ import annotations

from dataclasses import replace

from app.graphrag.ontology import Term
from app.graphrag.term_edits_store import (
    EXTRA_PROPERTY_PREFIX,
    FIELD_CREATED,
    FIELD_DELETED,
)

# Term 上可以被整字段替换的编辑字段。extra_properties 不在其中——它按
# "extra_properties.<name>" 的形式逐个属性编辑，见下面的说明。
_REPLACEABLE_FIELDS = ("standard_name", "aliases", "term_type")


def _apply_field_edits(term: Term, edits: dict[str, object]) -> Term:
    """把普通字段级编辑叠加到一个 Term 上，返回新的 Term。

    extra_properties 走单独的路径：编辑的 field 形如
    "extra_properties.revenue"，只覆盖字典里的那一个键，同一个字典里
    其余属性仍然跟随管道——这正是"字段级而不是整行级"的要点，整行覆盖
    会让人工只改了展示名却导致该实体的金额再也不跟着数据源更新。
    """
    changes: dict[str, object] = {}
    for field in _REPLACEABLE_FIELDS:
        if field in edits:
            changes[field] = edits[field]

    property_edits = {
        key[len(EXTRA_PROPERTY_PREFIX):]: value
        for key, value in edits.items()
        if key.startswith(EXTRA_PROPERTY_PREFIX)
    }
    if property_edits:
        changes["extra_properties"] = {**term.extra_properties, **property_edits}

    return replace(term, **changes) if changes else term


def _created_to_edits(created: dict[str, object]) -> dict[str, object]:
    """把 __created__ 的字段对象摊平成普通字段级编辑。

    这是"管道后来产出了同 node_key"那条语义的实现：__created__ 的每个
    字段等价于一条同字段的普通编辑，于是管道的行接管存在性、而人当初
    填的那些值继续按字段级优先。
    """
    flattened: dict[str, object] = {}
    for field in _REPLACEABLE_FIELDS:
        if field in created:
            flattened[field] = created[field]
    for name, value in (created.get("extra_properties") or {}).items():
        flattened[f"{EXTRA_PROPERTY_PREFIX}{name}"] = value
    return flattened


def _synthesize_created(
    node_key: str, created: dict[str, object], *, tenant_id: str
) -> Term:
    """terms 表里没有对应行时，由 __created__ 合成一个 Term。

    source 固定为 "review"：这条路径就是审核界面批准关系时现场创建端点
    实体用的，沿用既有的来源标记，让"哪些实体不是管道产出的"这个问题
    在合并视图上仍然可答。
    """
    return Term(
        tenant_id=tenant_id,
        node_key=node_key,
        standard_name=str(created.get("standard_name", "")),
        aliases=list(created.get("aliases") or []),
        term_type=str(created.get("term_type", "")),
        extra_properties=dict(created.get("extra_properties") or {}),
        source="review",
    )


def apply_edits(
    terms: list[Term], edits: dict[str, dict[str, object]], *, tenant_id: str
) -> list[Term]:
    """把编辑叠加到管道产出上，返回合并后的术语列表。

    terms 和 edits 都应当已经是同一个租户的（调用方负责按 tenant_id 查）。

    产出顺序：先是 terms 的顺序（调用方通常按 standard_name 排过），再是
    纯由编辑层创建、terms 表里没有对应行的那些。
    """
    merged: list[Term] = []
    seen: set[str] = set()

    for term in terms:
        node_edits = edits.get(term.node_key)
        seen.add(term.node_key)
        if node_edits is None:
            merged.append(term)
            continue
        if FIELD_DELETED in node_edits:
            # 人工删除不可被管道恢复。terms 表里的行仍然存在（ETL 还在
            # 维护它），只是对所有读路径不可见。
            continue
        effective = dict(node_edits)
        created = effective.pop(FIELD_CREATED, None)
        if created is not None:
            # 管道后来产出了同 node_key：管道的行接管存在性，__created__
            # 里记录的字段降级为普通字段级编辑。普通编辑优先级更高——它
            # 是在创建之后发生的更新。
            effective = {**_created_to_edits(created), **effective}
        merged.append(_apply_field_edits(term, effective))

    for node_key, node_edits in edits.items():
        if node_key in seen:
            continue
        if FIELD_DELETED in node_edits:
            continue
        created = node_edits.get(FIELD_CREATED)
        if created is None:
            # 孤儿编辑：挂在一个 terms 表里不存在、也没有 __created__ 的
            # node_key 上。不凭空造实体——这种编辑通常来自实体被 ETL 的
            # sweep 清理之后，源里若再出现同 node_key，它会自动重新生效。
            continue
        synthesized = _synthesize_created(node_key, created, tenant_id=tenant_id)
        rest = {k: v for k, v in node_edits.items() if k != FIELD_CREATED}
        merged.append(_apply_field_edits(synthesized, rest) if rest else synthesized)

    return merged
```

- [ ] **Step 4: 在 `terms_store.py` 加两个合并查询**

在 `list_terms`（`:305`）之后加：

```python
async def list_terms_merged(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    source: str | None = None,
) -> list[Term]:
    """管道产出叠加人工编辑之后的术语列表——**所有读路径都该走这个**，
    而不是 list_terms。

    参数与 list_terms 一致。注意 limit/offset 作用在 terms 表的查询上，
    合并发生在之后：被 __deleted__ 排除掉的行会让这一页少几条，纯编辑层
    创建的实体则追加在末尾。分页的精确性让位于"读到的一定是合并结果"
    ——后者是本设计的保证，前者只是列表页的观感。
    """
    terms = await list_terms(conn, tenant_id, limit=limit, offset=offset, source=source)
    edits = await list_term_edits(conn, tenant_id)
    return apply_edits(terms, edits, tenant_id=tenant_id)


async def get_term_merged_by_node_key(
    conn: aiosqlite.Connection, tenant_id: str, node_key: str
) -> Term:
    """按 node_key 取单条的合并结果。

    实体被 __deleted__ 编辑标记过时抛 TermNotFoundError——对读路径而言
    它就是不存在，跟 terms 表里根本没有这一行不该有可观测的区别。
    """
    edits = await list_term_edits_for_node_key(conn, tenant_id, node_key)
    try:
        term = await get_term_by_node_key(conn, tenant_id, node_key)
    except TermNotFoundError:
        merged = apply_edits([], {node_key: edits}, tenant_id=tenant_id)
        if not merged:
            raise
        return merged[0]
    merged = apply_edits([term], {node_key: edits}, tenant_id=tenant_id)
    if not merged:
        raise TermNotFoundError(f"术语已被人工删除: {node_key}")
    return merged[0]
```

import 区加：

```python
from app.graphrag.term_edits_store import list_term_edits, list_term_edits_for_node_key
from app.graphrag.term_merge import apply_edits
```

**注意循环导入**：`term_merge` 导入 `ontology.Term` 和 `term_edits_store` 的常量，`terms_store` 导入 `term_merge`——不构成环。如果实际出现循环导入，把 `list_terms_merged` / `get_term_merged_by_node_key` 移到 `term_merge.py` 里（它已经导入了 `Term`），并在报告里说明。

- [ ] **Step 5: 跑测试，再跑全量**

Expected: `tests/graphrag/test_term_merge.py` `11 passed`；全量 `1489 passed`（1478 + 11），0 failed。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/term_merge.py app/graphrag/terms_store.py tests/graphrag/test_term_merge.py
git commit -m "feat(graphrag): merge pipeline output with manual edits on read"
```

---

## Task 3: 读路径批量改道

**Files:**
- Modify（每处把 `list_terms(` 换成 `list_terms_merged(`，并相应改 import）：
  - `app/api/admin_document_routes.py`（2 处）
  - `app/api/admin_graph_review_routes.py`（1 处）
  - `app/api/agent_routes.py`（1 处）
  - `app/api/qa_routes.py`（1 处）
  - `app/api/voice_routes.py`（1 处）
  - `app/eval/runner.py`（1 处）
  - `app/graphrag/duplicate_detection_worker.py`（1 处）
  - `app/graphrag/review_cli.py`（1 处）
  - `app/ingestion/incremental_main.py`（1 处）
  - `app/ingestion/main.py`（1 处）

**Interfaces:**
- Consumes: `list_terms_merged(conn, tenant_id, *, limit=None, offset=0, source=None) -> list[Term]`（Task 2）

**这是一次机械替换，但漏掉一处是静默的**——漏掉的地方会读到未合并的原始数据，而那是合法的旧值，不报错。

**不要改的两处：**
- `app/graphrag/terms_store.py` 内部的两处 `list_terms(conn, tenant_id)` 调用（在 `merge_terms` 里）——Task 5 会整个重写 `merge_terms`。
- `app/api/admin_terms_routes.py` 的三处——Task 4 一并处理（它同时要改写入端点）。

- [ ] **Step 1: 逐个文件替换**

对上面列出的每个文件：把 `from app.graphrag.terms_store import ... list_terms ...` 改成导入 `list_terms_merged`，把调用点改名。**参数不变**（签名一致）。

替换后用这条命令确认没有漏网的：

```bash
grep -rn "list_terms(" --include=*.py app/ | grep -v "list_terms_merged(" | grep -v "def list_terms"
```

Expected: 只剩 `app/graphrag/terms_store.py`（2 处，`merge_terms` 内部）和 `app/api/admin_terms_routes.py`（3 处）。**其余任何一处命中都是漏改。**

- [ ] **Step 2: 跑全量**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `1489 passed`，0 failed（纯改道，+0 测试）。

**如果有既有用例失败**：多半是那条用例的夹具没建 `term_edits` 表（`ensure_term_edits_schema` 只在 `open_ontology_store_conn` 里调，测试里的 `:memory:` 连接是各自建表的）。**修法是给夹具补建表调用，不是把调用点改回 `list_terms`。** 改回去就等于放弃这条改道。

- [ ] **Step 3: 提交**

```bash
git add app/api app/eval app/graphrag app/ingestion
git commit -m "refactor(graphrag): route reads through the merged term view"
```

---

## Task 4: 管理后台三个写入端点改写编辑层

**Files:**
- Modify: `app/api/admin_terms_routes.py`（POST `:126`、PUT `:187`、DELETE `:261`，以及两处读端点）
- Test: `tests/api/test_admin_terms_routes.py`

**Interfaces:**
- Consumes: `upsert_term_edit(conn, *, tenant_id, node_key, field, value, edited_by)`、`FIELD_CREATED`、`FIELD_DELETED`、`EXTRA_PROPERTY_PREFIX`（Task 1）；`list_terms_merged` / `get_term_merged_by_node_key`（Task 2）

**这个任务是 Global Constraints 第一条的落点：这三个端点之后永不写 `terms`。**

**修正 spec 的一处疏漏**：spec 的写路径表只列了 PUT/DELETE，漏了 POST。但 `create_term` 全库只有一个生产调用点——就是这里的 POST（`admin_terms_routes.py:151`），而"审核界面现场创建实体"走的正是它（见 `terms_store.py:563-568` 的文档字符串）。按 Global Constraints 第一条，POST 必须写 `__created__`。

**三个端点的新行为：**

| 端点 | 写什么 |
|---|---|
| POST | 一条 `__created__` 编辑，value 是 `{standard_name, term_type, aliases, extra_properties}`。`node_key` 仍按 `f"{term_type}:{standard_name}"` 生成（沿用 `create_term` 的规则）。 |
| PUT | 按提交的字段逐条写：`standard_name`、`aliases`、`term_type` 各一条；`extra_properties` 里每个键一条 `extra_properties.<name>`。**payload 里 `extra_properties` 缺席时不写任何属性编辑**（沿用既有语义：字段缺席=保留原值）。 |
| DELETE | 一条 `__deleted__` 编辑。**不删 `terms` 行。** |

**`edited_by` 取什么值**：这三个端点都在 `require_admin_session` 之下。请查看该依赖是否提供了可用的身份标识；有就用它，没有就用固定值 `"admin"` 并在报告里说明——本设计不做审计流水，`edited_by` 目前只是可观测性字段。

**DELETE 的图谱侧行为保持不变**：仍然调 `delete_term_node`（合并视图里该实体已不可见，图谱要跟上）。既有的"删除前按图谱边数做 409 检查"也保持不变。

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_terms_routes.py` 追加（**先读该文件既有用例，沿用它的客户端夹具与 `dependency_overrides` 风格**）：

```python
async def test_put_writes_an_edit_and_never_touches_the_terms_table(...):
    """Global Constraints 第一条：人工编辑路径永不写 terms。违反了的话
    "重跑 ETL 不伤人工修正"的保证就静默失效了。"""
    # 先用 ETL 路径（upsert_term_with_node_key）写一条实体
    # PUT 改展示名
    # 断言：terms 表里那一行的 standard_name **没变**
    # 断言：term_edits 里有一条 (node_key, "standard_name") 编辑
    # 断言：读端点返回的是人工值（走合并视图）


async def test_delete_writes_a_deleted_edit_and_keeps_the_terms_row(...):
    """人工删除不可被 ETL 恢复——terms 行仍然存在（ETL 还在维护它），
    但对所有读路径不可见。"""
    # 断言：terms 表里那一行**仍在**
    # 断言：term_edits 里有 __deleted__
    # 断言：读端点看不到它


async def test_post_writes_a_created_edit_not_a_terms_row(...):
    # 断言：terms 表里没有新增行
    # 断言：term_edits 里有 __created__，value 含提交的四个字段
    # 断言：读端点能看到这个新实体


async def test_put_only_writes_edits_for_the_fields_actually_submitted(...):
    """字段级而不是整行级。payload 里 extra_properties 缺席时不写任何
    属性编辑——否则该实体的属性值再也不跟着数据源更新。"""
    # 提交只含 standard_name/aliases/term_type 的 payload
    # 断言：term_edits 里没有任何 extra_properties.* 行
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改写三个端点**

按上表实现。同时把该文件里三处 `list_terms(` 改成 `list_terms_merged(`，把按 node_key 取单条的地方改成 `get_term_merged_by_node_key`。

**注意 `create_term` / `update_term` / `delete_term` 在这个文件里应当不再被调用。** 改完用 `grep -n "create_term\|update_term\|delete_term\b" app/api/admin_terms_routes.py` 确认，剩下的只该是 import 行（如果连 import 都不需要了就删掉）。

**`_check_name_conflict` 的去向**：`create_term` / `update_term` 内部做的名字冲突检查，在编辑层路径上不再发生。这是**刻意的**——合并视图里名字冲突不再是数据完整性问题（`standard_name` 早已不是身份键，2026-08-30 起允许重名）。请在端点的文档字符串里写明这个变化，不要试图在编辑层重建这道检查。

- [ ] **Step 4: 跑全量**

Expected: `1493 passed`（1489 + 4），0 failed。

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_terms_routes.py tests/api/test_admin_terms_routes.py
git commit -m "feat(api): write admin term edits to the edits layer"
```

---

## Task 5: `merge_terms` 改写编辑层

**Files:**
- Modify: `app/graphrag/terms_store.py`（`merge_terms`）
- Test: `tests/graphrag/test_terms_store.py`、`tests/graphrag/test_duplicate_review_queue.py`（按实际受影响的文件）

**Interfaces:**
- Consumes: `upsert_term_edit`、`FIELD_DELETED`（Task 1）；`get_term_merged_by_node_key`（Task 2）
- Produces: `merge_terms(conn, *, tenant_id, keep_node_key, merged_node_key)` 签名不变，行为改为只写编辑层

**这一处改完会变简单，不是变复杂。** `merge_terms` 现在那套"墓碑化 merged → 追加别名到 keep → 失败补偿恢复 → 补偿也失败时记 ERROR 日志"的机制，**唯一存在理由是绕开 `_check_name_conflict`**：不先把 merged 那条墓碑化，把它的 `standard_name` 追加成 keep 的别名就会撞上冲突检查。

编辑层没有这道检查。于是合并变成两次独立、幂等的编辑写入：

```
1. merged_node_key 写一条 __deleted__
2. keep_node_key 写一条 aliases 编辑 = keep 当前别名 + merged 的 standard_name + merged 的全部别名（去重）
```

没有顺序依赖，没有中间态，**补偿回滚整个不需要**。`_tombstone_name` 这个辅助函数如果没有别的调用方，一并删掉。

**两条要保住的既有语义：**
- merged 那条的 `terms` 行**不删**（`node_key` 可能已被 Neo4j 图数据引用）。现在这一条天然成立——编辑层根本不碰 `terms`。
- merged 那条自己的别名要一起追加进 keep，不是只追加 `standard_name`；否则 merged 的别名会变成孤儿，`resolve_term` 再也找不回它们。

**两个 node_key 有任意一个不存在时仍抛 `TermNotFoundError`**，签名与异常契约不变——`approve_duplicate_suggestion`（`duplicate_review_queue.py:156`）依赖它把这个异常翻译成自己的 `DuplicateReviewNotFoundError`。

- [ ] **Step 1: 改测试再改实现**

先读 `tests/graphrag/test_terms_store.py` 里覆盖 `merge_terms` 的既有用例。它们大概率断言了墓碑名、断言了补偿回滚行为——**那些断言的对象消失了**，需要改写成对编辑层的断言：

- merged 那条在**合并视图**里不可见（`list_terms_merged` 不含它），但 `terms` 表里仍在
- keep 那条在合并视图里的 `aliases` 含 merged 的 standard_name 和它原本的全部别名
- `term_edits` 里有且只有两条：merged 的 `__deleted__`、keep 的 `aliases`

**补偿回滚的用例整条删除是正当的**——被测行为已经不存在了。请在报告里逐条说明删了哪些、为什么。

再补一条新用例：

```python
async def test_merge_terms_is_idempotent_when_run_twice():
    """两次编辑写入各自幂等，重复合并同一对不会产生重复别名或异常——
    这是改到编辑层之后天然获得的性质，旧的墓碑化实现做不到。"""
```

- [ ] **Step 2: 重写 `merge_terms`**

保留原函数的文档字符串里仍然成立的部分（merged 那条不删、别名要一起追加的理由），删掉已经不适用的部分（墓碑化、补偿回滚、冲突检查），并写明"为什么现在不需要补偿了"。

- [ ] **Step 3: 跑全量**

Expected: `1493 passed ± 你增删的用例数`，0 failed。用例数在报告里说明。

- [ ] **Step 4: 提交**

```bash
git add app/graphrag/terms_store.py tests/graphrag
git commit -m "refactor(graphrag): express term merges as edits, not tombstones"
```

---

## Task 6: Neo4j 同步走合并视图

**Files:**
- Modify: `app/api/admin_terms_routes.py`（`:177`、`:251` 两处 `sync_term`）
- Modify: `app/graphrag/schema_etl.py`（`:191` 的 `sync_term`）
- Test: `tests/graphrag/test_schema_etl.py`

**Interfaces:**
- Consumes: `get_term_merged_by_node_key(conn, tenant_id, node_key) -> Term`（Task 2）

**图谱是合并结果的投影，不是 `terms` 的投影。** 现在有三处 `sync_term` 调用点把**未合并**的 Term 推给图谱：

- `admin_terms_routes.py:177`（POST 之后）和 `:251`（PUT 之后）——Task 4 改完后这两处的 Term 要从合并视图取。
- `schema_etl.py:191`（ETL 写入实体之后）——**这一处最要紧**：ETL 刚写完的原始值会盖掉图上的人工修正。

**ETL 侧的实现要点**：`_write_entity_mapping` 现在直接用 `projected` 的值构造 `Term` 再 `sync_term`。改为写完 `upsert_term_with_node_key` 之后，用 `get_term_merged_by_node_key` 取回合并结果再同步。这会给每一行多一次查询——**先按最直接的写法实现并跑通**，性能问题留给报告记录，不要在本任务里提前优化成批量预取（那会改变错误处理的粒度）。

**`__deleted__` 的实体不同步**：`get_term_merged_by_node_key` 对被删除的实体抛 `TermNotFoundError`。ETL 路径遇到它时应当**跳过 `sync_term` 并在图上删除该节点**（人工删除不可被 ETL 恢复，图谱要跟上），而不是让异常中断整批。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_schema_etl.py` 追加：

```python
async def test_etl_sync_pushes_the_merged_value_not_the_raw_pipeline_value(tmp_path):
    """图谱是合并结果的投影。ETL 刚写完的原始值不能盖掉图上的人工修正。"""
    # ETL 写入实体 → 人工改展示名（写 term_edits）→ 重跑 ETL
    # 断言：FakeGraphClient 收到的最后一个 Term 的 standard_name 是人工值


async def test_etl_does_not_sync_a_manually_deleted_term_and_removes_its_node(tmp_path):
    """人工删除不可被 ETL 恢复——图谱要跟上，而不是让 ETL 把它同步回去。"""
    # ETL 写入 → 人工 __deleted__ → 重跑 ETL
    # 断言：该 node_key 不在 synced 里；在 deleted_nodes 里
```

**注意**：`FakeGraphClient` 目前只记录 `term.node_key`（`self.synced.append(term.node_key)`）。第一条用例要断言 `standard_name`，需要改成记录整个 `term` 或额外记一个 `synced_terms` 列表。**改的时候不要破坏既有用例对 `synced` 的断言**——加一个新列表比改既有列表的元素类型安全。

- [ ] **Step 2: 跑测试确认失败，再改三处调用点**

- [ ] **Step 3: 跑全量**

Expected: 基线 + 2，0 failed。

- [ ] **Step 4: 提交**

```bash
git add app/api/admin_terms_routes.py app/graphrag/schema_etl.py tests/graphrag/test_schema_etl.py
git commit -m "feat(graphrag): project the merged view into Neo4j, not raw terms"
```

---

## Task 7: 端到端核心保证

**Files:**
- Test: `tests/graphrag/test_manual_edits_integration.py`（新建）

**Interfaces:**
- Consumes: 前六个任务的全部产出

**这个任务只写测试，不改生产代码。** 本设计的核心保证是跨模块的——单个任务的测试证明不了"ETL 重跑不伤人工修正"，因为那条链路横跨编辑层、合并视图、ETL 写入和图谱同步。这是它单独成任务、单独过一次评审的理由。

如果这些测试里有任何一条**挂了**，那是前面某个任务的缺陷暴露出来了。**不要改测试去迁就**——报告里说明是哪条链路断了。

- [ ] **Step 1: 写四条端到端测试**

```python
async def test_etl_rerun_keeps_the_manual_display_name_and_updates_other_fields(tmp_path):
    """**本设计的核心保证。**

    写入实体 → 人工改展示名 → 源文件里改掉展示名和一个属性值 → 重跑 ETL
    → 断言展示名仍是人工值，而未编辑的属性字段取到了 ETL 的新值。
    """


async def test_manual_deletion_survives_an_etl_rerun(tmp_path):
    """人工删除 → 重跑 ETL → 断言该实体在合并视图和图谱里都不出现。
    今天的行为是它会复活（upsert 重新插入）。"""


async def test_editing_one_field_does_not_freeze_the_others(tmp_path):
    """字段级隔离：只编辑 standard_name，断言 extra_properties 仍随 ETL
    更新。防止实现退化成整行覆盖。"""


async def test_etl_path_never_writes_term_edits(tmp_path):
    """Global Constraints 第一条的直接断言：跑一整轮 ETL（含实体写入、
    关系写入、sweep），断言 term_edits 表一行没多。"""
```

**这四条要跑真实的 `run_schema_etl`**，用 `tests/graphrag/test_schema_etl.py` 里既有的 `_confirmed_conn()` 和 `FakeGraphClient` 风格的夹具（可以从那个文件导入，或按同样风格新建）。

- [ ] **Step 2: 跑全量**

Expected: 基线 + 4，0 failed。

- [ ] **Step 3: 提交**

```bash
git add tests/graphrag/test_manual_edits_integration.py
git commit -m "test(graphrag): pin the end-to-end manual-edit guarantees"
```

- [ ] **Step 4: 在报告里写明上线注意事项**

`terms` 表没有记录哪些字段是人工改过的（`source` 列只标记**创建**渠道）。因此：

> **本设计上线前已经存在的人工修正，在下一次 ETL 重跑时仍然会被覆盖一次；之后的修正才受保护。** 需要在上线前告知租户，或在上线前先跑一次 ETL 让两侧对齐。

本计划刻意**不**试图猜测哪些行被人工改过并回填成编辑——那只会制造错误的编辑记录，让本来正确的 ETL 更新被一条凭空捏造的"人工编辑"永久冻结。

---

## Self-Review

**1. Spec coverage**

| spec 章节 | 对应任务 |
|---|---|
| 数据模型（`term_edits` 表、六种 `field` 取值） | Task 1 |
| 合并语义 Apply User Edits | Task 2 |
| 删除不可被 ETL 恢复 | Task 2（合并）+ Task 7（端到端） |
| 编辑层创建的实体（无先例的那条判断） | Task 2 的 `test_created_then_etl_produces_the_same_node_key` |
| 写路径分化 · ETL 只写 `terms` | Task 7 的 `test_etl_path_never_writes_term_edits` |
| 写路径分化 · 管理后台 PUT/DELETE | Task 4 |
| 写路径分化 · 审核界面创建 | Task 4（与 POST 同一端点，见修正一） |
| 写路径分化 · `merge_terms` | Task 5 |
| 读路径统一走合并视图 | Task 2（提供）+ Task 3（13 处改道）+ Task 4（`admin_terms_routes` 的 3 处） |
| Neo4j 侧 | Task 6 |
| 迁移 · 建表进唯一入口 | Task 1 Step 4 |
| 迁移 · 存量无法回填 | Task 7 Step 4（报告说明，不写代码） |
| 测试策略的六条 | ETL 重跑不伤编辑→T7；删除不可恢复→T7；字段级隔离→T7；`__created__`+ETL→T2；写路径分化→T4/T5/T7；Neo4j 走合并视图→T6 |
| 未决风险 · 改动面最大、要穷举读路径 | Task 3 Step 1 给了穷举命令和期望输出 |
| 未决风险 · 合并视图性能 | Task 6 明确"先按最直接的写法实现，性能留给报告"；不默认加缓存（spec 要求） |
| 未决风险 · `merge_terms` 重新表达 | Task 5，并修正了 spec 对其复杂度的判断（见修正二） |
| 未决风险 · `__created__` 无先例 | Task 2 的专门用例 |
| 未决风险 · 不做编辑历史 | Task 1 的 `upsert_term_edit` 文档字符串写明 |

**2. Placeholder scan**：Task 4、Task 6、Task 7 的测试给的是骨架加断言清单而非可粘贴代码——那三处的测试必须贴合各自文件既有的夹具风格（`dependency_overrides`、`_confirmed_conn()`、`FakeGraphClient`），我凭记忆写出完整代码大概率与实际夹具对不上、反而制造计划缺陷。每处都写明了必须断言什么。Task 4 的 `edited_by` 取值要求实施者查 `require_admin_session` 后决定并在报告说明。其余步骤均给了完整代码。

**3. Type consistency**：`apply_edits(terms, edits, *, tenant_id)` 在 Task 2 定义、被 `list_terms_merged` / `get_term_merged_by_node_key` 消费；`list_term_edits` 的返回类型 `dict[str, dict[str, object]]` 与 `apply_edits` 的 `edits` 入参一致；`list_term_edits_for_node_key` 的 `dict[str, object]` 在 `get_term_merged_by_node_key` 里被包成 `{node_key: edits}` 后传入，形状对得上；`FIELD_DELETED` / `FIELD_CREATED` / `EXTRA_PROPERTY_PREFIX` 三个常量在 Task 1 定义，Task 2、4、5 消费；`list_terms_merged` 的签名与 `list_terms` 逐字一致，所以 Task 3 是纯改名。

**4. 测试计数链**：1471（基线）→ 1478（T1，+7）→ 1489（T2，+11）→ 1489（T3，+0，纯改道）→ 1493（T4，+4）→ T5（±，改写既有用例，实施者报告实际值）→ T6（+2）→ T7（+4）。
