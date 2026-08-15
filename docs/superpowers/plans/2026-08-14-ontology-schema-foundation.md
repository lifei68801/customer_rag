# 知识图谱本体 Schema 基座（数据模型 + CRUD API + 孤点保护）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `docs/superpowers/specs/2026-08-14-ontology-schema-design.md` 里"本体
schema"的数据模型和后端 CRUD API——分类（term_type/product_line，含属性字段声明）、
租户级关系类型、domain/range 约束表、草稿/确认生命周期、孤点数据保护规则。**不包含**
抽取管线的门禁/动态化改造（留给后续计划），**不包含**前端图形界面（留给后续计划）。

**Architecture:** 分类层（`ontology_categories.py`）全局共享、立即生效，与已上线的
`terms` 表同一作用域，不进草稿/确认流程。关系类型层（`ontology_relations.py`）和约束层
（`ontology_constraints.py`）按租户隔离，进草稿/确认两态生命周期，由
`ontology_lifecycle.py` 统一编排"检出草稿"和"确认发布"两个跨表原子操作。所有新表都建在
`get_review_conn` 现有的那个 SQLite 连接（`graph_review_db_path`）上，与 `terms`/
`review_queue` 表同库，不新开数据库文件。

**Tech Stack:** FastAPI + aiosqlite（沿用现有 `terms_store.py`/`review_queue.py` 的
idempotent-schema + 应用层校验模式），Neo4j（关系类型改名的边迁移）。

## Global Constraints

- 分类层（`ontology_term_types`/`ontology_product_lines`）**全局不分租户**，不进草稿/
  确认流程，CRUD 立即生效——与已上线的 `terms` 表同一作用域，理由见 spec 文档第 3 节的
  修订说明。
- 关系类型层（`tenant_relation_types`）和约束层（`term_type_relation_allowlist`）**按
  租户隔离**，进草稿（`status='draft'`）/确认（`status='confirmed'`）两态生命周期。
- 关系类型名字必须满足标识符格式 `^[A-Z][A-Z0-9_]{0,63}$`（Cypher 注入防线，机械校验，
  不可关闭，不需要人工审核）。
- 关系类型/约束的新增、修改、删除**不需要工程师审核**——图形化确认界面（后续计划实现）
  本身就是安全网。
- 属性字段（`extra_fields`）和属性值（`extra_properties`）都是自由文本，不引入类型
  系统（不做枚举/数字校验）。
- 删除/改名对已有数据的影响，严格按 spec 文档第 7 节"孤点数据保护规则"表格执行：
  - 删除 `term_type`/`product_line` 枚举值：**阻止**，需先把引用它的术语改到其他类型。
  - 删除关系类型：**允许**，已写入 Neo4j 的旧边保留原类型不变。
  - 改名关系类型：**不自动迁移**，提供可选的后台迁移任务，由业务显式触发。
  - 移除属性字段：**保留**节点上已写入的旧属性值，仅编辑界面不再显示。
  - 修改约束表：不影响已写入的旧边，只影响以后新抽取的候选。
- 所有新 SQLite 表建在 `get_review_conn`/`build_review_conn_from_settings` 现有连接上，
  复用 `app/graphrag/terms_store.py` 建立的 idempotent-schema（`CREATE TABLE IF NOT
  EXISTS`）+ 应用层校验（不依赖 SQLite 外键约束）模式。
- 已上线的 `terms` 表补一道向后兼容的桥接：部署这次改动时，如果分类枚举表是空的、但
  `terms` 表已经有历史数据，自动把历史数据里出现过的 `term_type`/`product_line` 去重
  导入枚举表——否则硬约束上线的第一刻，任何现有术语的编辑请求都会因为找不到匹配的枚举
  值而报错，这是一个真实的部署风险，不是假设性问题。

---

### Task 1: 分类层——`ontology_categories.py`

**Files:**
- Create: `app/graphrag/ontology_categories.py`
- Test: `tests/graphrag/test_ontology_categories.py`

**Interfaces:**
- Consumes: 无（新模块，不依赖本计划其他任务）。
- Produces：
  - `ensure_categories_schema(conn: aiosqlite.Connection) -> None`
  - `@dataclass(frozen=True) class TermTypeCategory: value: str; extra_fields: list[str]`
  - `list_term_types(conn) -> list[TermTypeCategory]`
  - `list_product_lines(conn) -> list[str]`
  - `create_term_type(conn, *, value: str, extra_fields: list[str] | None = None) -> None`
  - `create_product_line(conn, *, value: str) -> None`
  - `update_term_type(conn, *, value: str, new_value: str, extra_fields: list[str]) -> None`
  - `update_product_line(conn, *, value: str, new_value: str) -> None`
  - `delete_term_type(conn, value: str) -> None`
  - `delete_product_line(conn, value: str) -> None`
  - `CategoryNotFoundError`, `CategoryInUseError`, `CategoryNameConflictError`（均继承
    `Exception`）
  - 供后续任务复用：Task 6（`terms_store.py`）用 `list_term_types`/`list_product_lines`
    做写入校验；Task 3（`ontology_constraints.py`）用 `list_term_types` 校验约束表引用
    的类型是否存在。

- [ ] **Step 1: 写失败测试——建表 + 基础 CRUD**

```python
# tests/graphrag/test_ontology_categories.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    TermTypeCategory,
    create_product_line,
    create_term_type,
    delete_product_line,
    delete_term_type,
    ensure_categories_schema,
    list_product_lines,
    list_term_types,
    update_product_line,
    update_term_type,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_categories_schema(conn)
    return conn


async def test_create_and_list_term_type_with_extra_fields():
    conn = await _conn()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级", "影响范围"])

    result = await list_term_types(conn)

    assert result == [TermTypeCategory(value="错误码", extra_fields=["严重等级", "影响范围"])]


async def test_create_term_type_without_extra_fields_defaults_to_empty_list():
    conn = await _conn()
    await create_term_type(conn, value="地点")

    result = await list_term_types(conn)

    assert result == [TermTypeCategory(value="地点", extra_fields=[])]


async def test_create_and_list_product_line():
    conn = await _conn()
    await create_product_line(conn, value="示例产品线")

    assert await list_product_lines(conn) == ["示例产品线"]


async def test_create_duplicate_term_type_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, value="错误码")

    with pytest.raises(CategoryNameConflictError):
        await create_term_type(conn, value="错误码")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/graphrag/test_ontology_categories.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.graphrag.ontology_categories'`）

- [ ] **Step 3: 实现建表与基础 CRUD**

```python
# app/graphrag/ontology_categories.py
from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    value        TEXT PRIMARY KEY,
    extra_fields TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS ontology_product_lines (
    value TEXT PRIMARY KEY
);
"""


class CategoryNotFoundError(Exception):
    """指定的分类枚举值不存在。"""


class CategoryInUseError(Exception):
    """删除的分类枚举值仍被 terms 表引用，terms.term_type/product_line 是硬约束外键，
    删除在用的值会让已有术语行结构失效，必须阻止（不同于关系类型删除——那只是写入
    白名单，不是任何表的外键约束对象，见 ontology_relations.py）。"""


class CategoryNameConflictError(Exception):
    """提交的分类值已存在。"""


@dataclass(frozen=True)
class TermTypeCategory:
    value: str
    extra_fields: list[str]


async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def _row_to_term_type(row: aiosqlite.Row) -> TermTypeCategory:
    return TermTypeCategory(value=row["value"], extra_fields=json.loads(row["extra_fields"]))


async def list_term_types(conn: aiosqlite.Connection) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields FROM ontology_term_types ORDER BY value"
    )
    rows = await cursor.fetchall()
    return [_row_to_term_type(row) for row in rows]


async def list_product_lines(conn: aiosqlite.Connection) -> list[str]:
    cursor = await conn.execute("SELECT value FROM ontology_product_lines ORDER BY value")
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def create_term_type(
    conn: aiosqlite.Connection, *, value: str, extra_fields: list[str] | None = None
) -> None:
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types (value, extra_fields) VALUES (?, ?)",
            (value, json.dumps(extra_fields or [], ensure_ascii=False)),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是已有分类，不能重复创建")
    await conn.commit()


async def create_product_line(conn: aiosqlite.Connection, *, value: str) -> None:
    try:
        await conn.execute(
            "INSERT INTO ontology_product_lines (value) VALUES (?)", (value,)
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是已有产品线，不能重复创建")
    await conn.commit()
```

- [ ] **Step 4: 运行测试确认基础 CRUD 通过**

Run: `pytest tests/graphrag/test_ontology_categories.py -v`
Expected: 前 4 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology_categories.py tests/graphrag/test_ontology_categories.py
git commit -m "feat(graphrag): add global term-type/product-line category tables"
```

- [ ] **Step 6: 写失败测试——改名级联 + 删除保护**

```python
async def test_update_term_type_renames_without_referencing_terms():
    conn = await _conn()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])

    await update_term_type(conn, value="错误码", new_value="故障码", extra_fields=["严重等级", "影响范围"])

    result = await list_term_types(conn)
    assert result == [TermTypeCategory(value="故障码", extra_fields=["严重等级", "影响范围"])]


async def test_update_term_type_cascades_rename_to_referencing_terms():
    conn = await _conn()
    await create_term_type(conn, value="错误码")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, term_type, product_line) VALUES (?, ?, ?)",
        ("错误码E502", "错误码", "示例产品线"),
    )
    await conn.commit()

    await update_term_type(conn, value="错误码", new_value="故障码", extra_fields=[])

    cursor = await conn.execute("SELECT term_type FROM terms WHERE standard_name = ?", ("错误码E502",))
    row = await cursor.fetchone()
    assert row[0] == "故障码"


async def test_update_term_type_into_existing_name_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, value="错误码")
    await create_term_type(conn, value="模块")

    with pytest.raises(CategoryNameConflictError):
        await update_term_type(conn, value="错误码", new_value="模块", extra_fields=[])


async def test_delete_term_type_not_in_use_succeeds():
    conn = await _conn()
    await create_term_type(conn, value="错误码")

    await delete_term_type(conn, "错误码")

    assert await list_term_types(conn) == []


async def test_delete_term_type_in_use_raises_conflict():
    conn = await _conn()
    await create_term_type(conn, value="错误码")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, term_type, product_line) VALUES (?, ?, ?)",
        ("错误码E502", "错误码", "示例产品线"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_term_type(conn, "错误码")


async def test_delete_product_line_in_use_raises_conflict():
    conn = await _conn()
    await create_product_line(conn, value="示例产品线")
    await conn.execute(
        "CREATE TABLE terms (standard_name TEXT PRIMARY KEY, term_type TEXT NOT NULL, "
        "product_line TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]')"
    )
    await conn.execute(
        "INSERT INTO terms (standard_name, term_type, product_line) VALUES (?, ?, ?)",
        ("错误码E502", "错误码", "示例产品线"),
    )
    await conn.commit()

    with pytest.raises(CategoryInUseError):
        await delete_product_line(conn, "示例产品线")
```

- [ ] **Step 7: 运行测试确认失败**

Run: `pytest tests/graphrag/test_ontology_categories.py -v`
Expected: 新增的 6 个测试 FAIL（`update_term_type`/`delete_term_type`/`delete_product_line`
未定义）

- [ ] **Step 8: 实现改名级联与删除保护**

在 `app/graphrag/ontology_categories.py` 追加：

```python
async def update_term_type(
    conn: aiosqlite.Connection, *, value: str, new_value: str, extra_fields: list[str]
) -> None:
    """value 是当前名字，new_value 是提交的新名字，允许相同（即不改名）。改名时级联
    更新 terms 表里所有引用旧名字的行——term_type 只是 Term 节点上的普通属性（随
    sync_term() 整体覆盖写入），不是节点身份标识，不需要像 standard_name 改名那样
    联动 Neo4j 节点属性更新（rename_term_node），下一次任何该术语的编辑都会用新
    term_type 重新 sync_term()。"""
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE value = ?", (value,)
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"分类不存在: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ? WHERE value = ?",
            (new_value, json.dumps(extra_fields, ensure_ascii=False), value),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是已有分类，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE terms SET term_type = ? WHERE term_type = ?", (new_value, value)
        )
    await conn.commit()


async def update_product_line(
    conn: aiosqlite.Connection, *, value: str, new_value: str
) -> None:
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_product_lines WHERE value = ?", (value,)
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"产品线不存在: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_product_lines SET value = ? WHERE value = ?", (new_value, value)
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是已有产品线，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE terms SET product_line = ? WHERE product_line = ?", (new_value, value)
        )
    await conn.commit()


async def delete_term_type(conn: aiosqlite.Connection, value: str) -> None:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE term_type = ?", (value,)
    )
    row = await cursor.fetchone()
    if row[0] > 0:
        raise CategoryInUseError(f"分类 {value!r} 仍被 {row[0]} 条术语引用，无法删除")
    await conn.execute("DELETE FROM ontology_term_types WHERE value = ?", (value,))
    await conn.commit()


async def delete_product_line(conn: aiosqlite.Connection, value: str) -> None:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE product_line = ?", (value,)
    )
    row = await cursor.fetchone()
    if row[0] > 0:
        raise CategoryInUseError(f"产品线 {value!r} 仍被 {row[0]} 条术语引用，无法删除")
    await conn.execute("DELETE FROM ontology_product_lines WHERE value = ?", (value,))
    await conn.commit()
```

- [ ] **Step 9: 运行完整测试文件确认全部通过**

Run: `pytest tests/graphrag/test_ontology_categories.py -v`
Expected: 全部 PASS

- [ ] **Step 10: 提交**

```bash
git add app/graphrag/ontology_categories.py tests/graphrag/test_ontology_categories.py
git commit -m "feat(graphrag): cascade-rename and reference-protect category deletes"
```

---

### Task 2: 关系类型层——`ontology_relations.py`

**Files:**
- Create: `app/graphrag/ontology_relations.py`
- Test: `tests/graphrag/test_ontology_relations.py`

**Interfaces:**
- Consumes: 无（新模块，不依赖 Task 1）。
- Produces：
  - `ensure_relations_schema(conn) -> None`
  - `@dataclass(frozen=True) class RelationTypeDef: relation_type: str; example_phrase: str; description: str; allow_chain_query: bool; source: str`
  - `list_relation_types(conn, tenant_id: str, *, status: str) -> list[RelationTypeDef]`
  - `seed_default_relation_types(conn, tenant_id: str) -> None`
  - `create_relation_type(conn, tenant_id, *, relation_type, example_phrase, description="", allow_chain_query=False) -> None`
  - `update_relation_type(conn, tenant_id, *, relation_type, example_phrase, description, allow_chain_query) -> None`
  - `delete_relation_type(conn, tenant_id, relation_type: str) -> None`
  - `InvalidRelationTypeNameError`, `RelationTypeNotFoundError`
  - 供后续任务复用：Task 3 校验约束表引用的关系类型是否存在于草稿中；Task 4
    （`ontology_lifecycle.py`）编排草稿检出/确认；Task 7 暴露成 REST API。

- [ ] **Step 1: 写失败测试——格式校验 + 默认种子 + 基础 CRUD**

```python
# tests/graphrag/test_ontology_relations.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    RelationTypeDef,
    RelationTypeNotFoundError,
    create_relation_type,
    delete_relation_type,
    ensure_relations_schema,
    list_relation_types,
    seed_default_relation_types,
    update_relation_type,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_relations_schema(conn)
    return conn


async def test_seed_default_relation_types_writes_ten_draft_rows():
    conn = await _conn()

    await seed_default_relation_types(conn, "t1")

    result = await list_relation_types(conn, "t1", status="draft")
    assert len(result) == 10
    assert all(r.source == "default" for r in result)
    chain_eligible = {r.relation_type for r in result if r.allow_chain_query}
    assert chain_eligible == {"REQUIRES", "PRECEDES", "PART_OF"}


async def test_seed_default_relation_types_is_idempotent():
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")

    await seed_default_relation_types(conn, "t1")

    assert len(await list_relation_types(conn, "t1", status="draft")) == 10


async def test_seed_is_scoped_per_tenant():
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")

    assert await list_relation_types(conn, "t2", status="draft") == []


async def test_create_relation_type_with_valid_name():
    conn = await _conn()

    await create_relation_type(
        conn, "t1", relation_type="SUITABLE_FOR", example_phrase="大床房 SUITABLE_FOR 家庭出行"
    )

    result = await list_relation_types(conn, "t1", status="draft")
    assert result == [
        RelationTypeDef(
            relation_type="SUITABLE_FOR",
            example_phrase="大床房 SUITABLE_FOR 家庭出行",
            description="",
            allow_chain_query=False,
            source="custom",
        )
    ]


async def test_create_relation_type_rejects_invalid_format():
    conn = await _conn()

    with pytest.raises(InvalidRelationTypeNameError):
        await create_relation_type(conn, "t1", relation_type="suitable-for", example_phrase="x")


async def test_create_relation_type_rejects_empty_example_phrase():
    conn = await _conn()

    with pytest.raises(InvalidRelationTypeNameError):
        await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/graphrag/test_ontology_relations.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现建表、格式校验、默认种子、创建**

```python
# app/graphrag/ontology_relations.py
from __future__ import annotations

import re
from dataclasses import dataclass

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_relation_types (
    tenant_id         TEXT NOT NULL,
    relation_type     TEXT NOT NULL,
    example_phrase    TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    allow_chain_query INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'custom',
    status            TEXT NOT NULL,
    PRIMARY KEY (tenant_id, relation_type, status)
);
"""

# Cypher 关系类型不能参数化绑定，只能拼进查询字符串——这是注入防线，任何写入路径
# （无论数据来自默认种子还是业务自助新增）都必须过这道机械校验，不因为是"默认值"
# 就豁免。
_RELATION_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# 10 种全局默认拓扑关系的初始种子——例句取自 app/graphrag/llm_extractor.py 现有
# system prompt 里的例句，保持口径一致。REQUIRES/PRECEDES/PART_OF 默认放开链式
# 查询资格，理由见 docs/superpowers/specs/2026-08-09-chunking-graph-extraction-
# redesign-design.md §5。
_DEFAULT_RELATION_TYPES: list[tuple[str, str, str, bool]] = [
    ("RELATED_TO", "促销活动 RELATED_TO 会员日", "兜底：弱关联，语义不明确时的默认选项", False),
    ("PART_OF", "客房 PART_OF 酒店", "部分-整体", True),
    ("IS_A", "大床房 IS_A 客房", "类别从属/分类层级", False),
    ("REQUIRES", "预订套餐 REQUIRES 会员资格", "前提依赖", True),
    ("ALTERNATIVE_TO", "标准间 ALTERNATIVE_TO 大床房", "替代/类似", False),
    ("CAUSES", "恶劣天气 CAUSES 接送延误", "因果", False),
    ("ADDRESSED_BY", "房间异味 ADDRESSED_BY 更换房间", "问题由方案解决", False),
    ("LOCATED_IN", "健身房 LOCATED_IN 三楼", "空间/组织归属", False),
    ("APPLIES_TO", "会员折扣 APPLIES_TO 非节假日预订", "适用范围", False),
    ("PRECEDES", "入住登记 PRECEDES 领取房卡", "流程先后顺序", True),
]


class InvalidRelationTypeNameError(Exception):
    """关系类型名字不满足标识符格式，或例句为空。"""


class RelationTypeNotFoundError(Exception):
    """指定租户的草稿里不存在这个关系类型。"""


@dataclass(frozen=True)
class RelationTypeDef:
    relation_type: str
    example_phrase: str
    description: str
    allow_chain_query: bool
    source: str


async def ensure_relations_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def _row_to_def(row: aiosqlite.Row) -> RelationTypeDef:
    return RelationTypeDef(
        relation_type=row["relation_type"],
        example_phrase=row["example_phrase"],
        description=row["description"],
        allow_chain_query=bool(row["allow_chain_query"]),
        source=row["source"],
    )


async def list_relation_types(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[RelationTypeDef]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT relation_type, example_phrase, description, allow_chain_query, source "
        "FROM tenant_relation_types WHERE tenant_id = ? AND status = ? ORDER BY relation_type",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [_row_to_def(row) for row in rows]


async def seed_default_relation_types(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """把 10 种默认关系类型写入该租户的草稿——INSERT OR IGNORE 保证重复调用幂等
    （已存在的行不会被覆盖，业务如果已经改过某个默认类型，重复调用不会把改动冲掉）。
    """
    for relation_type, example_phrase, description, allow_chain in _DEFAULT_RELATION_TYPES:
        await conn.execute(
            "INSERT OR IGNORE INTO tenant_relation_types "
            "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
            "source, status) VALUES (?, ?, ?, ?, ?, 'default', 'draft')",
            (tenant_id, relation_type, example_phrase, description, int(allow_chain)),
        )
    await conn.commit()


def _validate_relation_type(relation_type: str, example_phrase: str) -> None:
    if not _RELATION_TYPE_PATTERN.match(relation_type):
        raise InvalidRelationTypeNameError(
            f"关系类型名字不合法: {relation_type!r}，必须满足 ^[A-Z][A-Z0-9_]{{0,63}}$"
        )
    if not example_phrase.strip():
        raise InvalidRelationTypeNameError("example_phrase 不能为空")


async def create_relation_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    relation_type: str,
    example_phrase: str,
    description: str = "",
    allow_chain_query: bool = False,
) -> None:
    _validate_relation_type(relation_type, example_phrase)
    await conn.execute(
        "INSERT OR REPLACE INTO tenant_relation_types "
        "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
        "source, status) VALUES (?, ?, ?, ?, ?, 'custom', 'draft')",
        (tenant_id, relation_type, example_phrase, description, int(allow_chain_query)),
    )
    await conn.commit()
```

- [ ] **Step 4: 运行测试确认全部通过**

Run: `pytest tests/graphrag/test_ontology_relations.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology_relations.py tests/graphrag/test_ontology_relations.py
git commit -m "feat(graphrag): add tenant relation-type table with default seed"
```

- [ ] **Step 6: 写失败测试——更新与删除**

```python
async def test_update_relation_type_changes_example_and_chain_flag():
    conn = await _conn()
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x SUITABLE_FOR y")

    await update_relation_type(
        conn, "t1",
        relation_type="SUITABLE_FOR",
        example_phrase="大床房 SUITABLE_FOR 家庭出行",
        description="适合的出行类型",
        allow_chain_query=True,
    )

    result = await list_relation_types(conn, "t1", status="draft")
    assert result == [
        RelationTypeDef(
            relation_type="SUITABLE_FOR",
            example_phrase="大床房 SUITABLE_FOR 家庭出行",
            description="适合的出行类型",
            allow_chain_query=True,
            source="custom",
        )
    ]


async def test_update_nonexistent_relation_type_raises_not_found():
    conn = await _conn()

    with pytest.raises(RelationTypeNotFoundError):
        await update_relation_type(
            conn, "t1", relation_type="NOPE", example_phrase="x", description="",
            allow_chain_query=False,
        )


async def test_delete_relation_type_removes_default_row_without_protection():
    """关系类型删除不设引用保护——已写入 Neo4j 的旧边不受影响（见 spec 文档
    第 7 节孤点数据保护规则表格），这里只验证 schema 表本身的删除行为。"""
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")

    await delete_relation_type(conn, "t1", "PRECEDES")

    remaining = {r.relation_type for r in await list_relation_types(conn, "t1", status="draft")}
    assert "PRECEDES" not in remaining
    assert len(remaining) == 9


async def test_delete_relation_type_is_scoped_per_tenant():
    conn = await _conn()
    await seed_default_relation_types(conn, "t1")
    await seed_default_relation_types(conn, "t2")

    await delete_relation_type(conn, "t1", "PRECEDES")

    assert len(await list_relation_types(conn, "t2", status="draft")) == 10
```

- [ ] **Step 7: 运行测试确认失败**

Run: `pytest tests/graphrag/test_ontology_relations.py -v`
Expected: 新增 4 个测试 FAIL

- [ ] **Step 8: 实现更新与删除**

在 `app/graphrag/ontology_relations.py` 追加：

```python
async def update_relation_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    relation_type: str,
    example_phrase: str,
    description: str,
    allow_chain_query: bool,
) -> None:
    _validate_relation_type(relation_type, example_phrase)
    cursor = await conn.execute(
        "SELECT 1 FROM tenant_relation_types WHERE tenant_id = ? AND relation_type = ? "
        "AND status = 'draft'",
        (tenant_id, relation_type),
    )
    if await cursor.fetchone() is None:
        raise RelationTypeNotFoundError(f"草稿里不存在关系类型: {relation_type}")
    await conn.execute(
        "UPDATE tenant_relation_types SET example_phrase = ?, description = ?, "
        "allow_chain_query = ? WHERE tenant_id = ? AND relation_type = ? AND status = 'draft'",
        (example_phrase, description, int(allow_chain_query), tenant_id, relation_type),
    )
    await conn.commit()


async def delete_relation_type(
    conn: aiosqlite.Connection, tenant_id: str, relation_type: str
) -> None:
    """不设引用保护——关系类型表只是写入时的白名单闸门，不是任何表的外键约束
    对象；已写入 Neo4j 的旧边不因为闸门关闭而失效（见调用方 ontology_lifecycle.py
    以及 spec 文档第 7 节）。"""
    await conn.execute(
        "DELETE FROM tenant_relation_types WHERE tenant_id = ? AND relation_type = ? "
        "AND status = 'draft'",
        (tenant_id, relation_type),
    )
    await conn.commit()
```

- [ ] **Step 9: 运行完整测试文件确认全部通过**

Run: `pytest tests/graphrag/test_ontology_relations.py -v`
Expected: 全部 PASS

- [ ] **Step 10: 提交**

```bash
git add app/graphrag/ontology_relations.py tests/graphrag/test_ontology_relations.py
git commit -m "feat(graphrag): add relation-type update/delete, no reference protection"
```

---

### Task 3: 约束层——`ontology_constraints.py`

**Files:**
- Create: `app/graphrag/ontology_constraints.py`
- Test: `tests/graphrag/test_ontology_constraints.py`

**Interfaces:**
- Consumes：
  - Task 1: `app.graphrag.ontology_categories.list_term_types(conn) -> list[TermTypeCategory]`
  - Task 2: `app.graphrag.ontology_relations.list_relation_types(conn, tenant_id, *, status) -> list[RelationTypeDef]`
- Produces：
  - `ensure_constraints_schema(conn) -> None`
  - `@dataclass(frozen=True) class AllowedCombination: subject_term_type: str; relation_type: str; object_term_type: str`
  - `list_allowed_combinations(conn, tenant_id, *, status) -> list[AllowedCombination]`
  - `add_allowed_combination(conn, tenant_id, *, subject_term_type, relation_type, object_term_type) -> None`
  - `remove_allowed_combination(conn, tenant_id, *, subject_term_type, relation_type, object_term_type) -> None`
  - `UnknownCategoryError`, `UnknownRelationTypeError`
  - 供后续任务复用：Task 4 编排草稿检出/确认；Task 7 暴露成 REST API；后续"抽取管线
    动态化"计划用 `list_allowed_combinations(status="confirmed")` 做写入时硬约束校验。

- [ ] **Step 1: 写失败测试——校验引用有效性 + 基础增删**

```python
# tests/graphrag/test_ontology_constraints.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_categories import create_term_type, ensure_categories_schema
from app.graphrag.ontology_constraints import (
    AllowedCombination,
    UnknownCategoryError,
    UnknownRelationTypeError,
    add_allowed_combination,
    ensure_constraints_schema,
    list_allowed_combinations,
    remove_allowed_combination,
)
from app.graphrag.ontology_relations import create_relation_type, ensure_relations_schema

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_categories_schema(conn)
    await ensure_relations_schema(conn)
    await ensure_constraints_schema(conn)
    return conn


async def test_add_allowed_combination_with_valid_references():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="客房 PART_OF 酒店")

    await add_allowed_combination(
        conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店"
    )

    result = await list_allowed_combinations(conn, "t1", status="draft")
    assert result == [
        AllowedCombination(subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")
    ]


async def test_add_allowed_combination_rejects_unknown_subject_type():
    conn = await _conn()
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="x")

    with pytest.raises(UnknownCategoryError):
        await add_allowed_combination(
            conn, "t1", subject_term_type="不存在的分类", relation_type="PART_OF",
            object_term_type="酒店",
        )


async def test_add_allowed_combination_rejects_unknown_relation_type():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")

    with pytest.raises(UnknownRelationTypeError):
        await add_allowed_combination(
            conn, "t1", subject_term_type="客房", relation_type="NOT_SEEDED",
            object_term_type="酒店",
        )


async def test_add_allowed_combination_is_idempotent():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="x")

    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")
    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    assert len(await list_allowed_combinations(conn, "t1", status="draft")) == 1


async def test_remove_allowed_combination():
    conn = await _conn()
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await create_relation_type(conn, "t1", relation_type="PART_OF", example_phrase="x")
    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    await remove_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    assert await list_allowed_combinations(conn, "t1", status="draft") == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/graphrag/test_ontology_constraints.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# app/graphrag/ontology_constraints.py
from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_relations import list_relation_types

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS term_type_relation_allowlist (
    tenant_id          TEXT NOT NULL,
    subject_term_type  TEXT NOT NULL,
    relation_type       TEXT NOT NULL,
    object_term_type   TEXT NOT NULL,
    status              TEXT NOT NULL,
    PRIMARY KEY (tenant_id, subject_term_type, relation_type, object_term_type, status)
);
"""


class UnknownCategoryError(Exception):
    """引用的 term_type 不在全局分类枚举里。"""


class UnknownRelationTypeError(Exception):
    """引用的关系类型不在该租户当前草稿里。"""


@dataclass(frozen=True)
class AllowedCombination:
    subject_term_type: str
    relation_type: str
    object_term_type: str


async def ensure_constraints_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def list_allowed_combinations(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[AllowedCombination]:
    cursor = await conn.execute(
        "SELECT subject_term_type, relation_type, object_term_type "
        "FROM term_type_relation_allowlist WHERE tenant_id = ? AND status = ? "
        "ORDER BY subject_term_type, relation_type, object_term_type",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [AllowedCombination(subject_term_type=r[0], relation_type=r[1], object_term_type=r[2]) for r in rows]


async def _validate_references(
    conn: aiosqlite.Connection, tenant_id: str, *, subject_term_type: str,
    relation_type: str, object_term_type: str,
) -> None:
    known_types = {c.value for c in await list_term_types(conn)}
    if subject_term_type not in known_types:
        raise UnknownCategoryError(f"未知分类: {subject_term_type!r}")
    if object_term_type not in known_types:
        raise UnknownCategoryError(f"未知分类: {object_term_type!r}")
    known_relations = {
        r.relation_type for r in await list_relation_types(conn, tenant_id, status="draft")
    }
    if relation_type not in known_relations:
        raise UnknownRelationTypeError(f"该租户草稿里不存在关系类型: {relation_type!r}")


async def add_allowed_combination(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    subject_term_type: str,
    relation_type: str,
    object_term_type: str,
) -> None:
    await _validate_references(
        conn, tenant_id, subject_term_type=subject_term_type,
        relation_type=relation_type, object_term_type=object_term_type,
    )
    await conn.execute(
        "INSERT OR IGNORE INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, 'draft')",
        (tenant_id, subject_term_type, relation_type, object_term_type),
    )
    await conn.commit()


async def remove_allowed_combination(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    subject_term_type: str,
    relation_type: str,
    object_term_type: str,
) -> None:
    await conn.execute(
        "DELETE FROM term_type_relation_allowlist WHERE tenant_id = ? AND "
        "subject_term_type = ? AND relation_type = ? AND object_term_type = ? AND status = 'draft'",
        (tenant_id, subject_term_type, relation_type, object_term_type),
    )
    await conn.commit()
```

- [ ] **Step 4: 运行测试确认全部通过**

Run: `pytest tests/graphrag/test_ontology_constraints.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology_constraints.py tests/graphrag/test_ontology_constraints.py
git commit -m "feat(graphrag): add domain/range allowlist table with reference validation"
```

---

### Task 4: 草稿/确认生命周期编排——`ontology_lifecycle.py`

**Files:**
- Create: `app/graphrag/ontology_lifecycle.py`
- Test: `tests/graphrag/test_ontology_lifecycle.py`

**Interfaces:**
- Consumes：Task 1/2/3 的 `ensure_*_schema`/`list_*`/`seed_default_relation_types`。
- Produces：
  - `ensure_ontology_schema(conn) -> None`（统一入口，供 Task 6/7 和后续计划的
    `deps.py`/`review_factory.py` 接线调用）
  - `checkout_draft(conn, tenant_id) -> None`
  - `confirm_ontology(conn, tenant_id) -> None`
  - `is_ontology_confirmed(conn, tenant_id) -> bool`（供后续"抽取管线动态化"计划的
    门禁检查点调用，本计划只负责实现和测试这个函数本身，不接入 pipeline）

- [ ] **Step 1: 写失败测试——检出草稿（含首次种子）与确认发布**

```python
# tests/graphrag/test_ontology_lifecycle.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_constraints import add_allowed_combination, list_allowed_combinations
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
    is_ontology_confirmed,
)
from app.graphrag.ontology_relations import create_relation_type, list_relation_types

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


async def test_checkout_draft_seeds_defaults_for_brand_new_tenant():
    conn = await _conn()

    await checkout_draft(conn, "t1")

    result = await list_relation_types(conn, "t1", status="draft")
    assert len(result) == 10


async def test_is_ontology_confirmed_false_before_first_confirm():
    conn = await _conn()
    await checkout_draft(conn, "t1")

    assert await is_ontology_confirmed(conn, "t1") is False


async def test_confirm_ontology_promotes_draft_to_confirmed():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_relation_type(conn, "t1", relation_type="SUITABLE_FOR", example_phrase="x")

    await confirm_ontology(conn, "t1")

    confirmed = await list_relation_types(conn, "t1", status="confirmed")
    assert len(confirmed) == 11
    assert await is_ontology_confirmed(conn, "t1") is True


async def test_confirm_ontology_clears_draft():
    conn = await _conn()
    await checkout_draft(conn, "t1")

    await confirm_ontology(conn, "t1")

    assert await list_relation_types(conn, "t1", status="draft") == []


async def test_confirm_ontology_replaces_previous_confirmed_version():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")
    await checkout_draft(conn, "t1")
    from app.graphrag.ontology_relations import delete_relation_type
    await delete_relation_type(conn, "t1", "PRECEDES")

    await confirm_ontology(conn, "t1")

    confirmed = {r.relation_type for r in await list_relation_types(conn, "t1", status="confirmed")}
    assert "PRECEDES" not in confirmed
    assert len(confirmed) == 9


async def test_checkout_draft_after_confirm_copies_confirmed_into_new_draft():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await confirm_ontology(conn, "t1")

    await checkout_draft(conn, "t1")

    draft = await list_relation_types(conn, "t1", status="draft")
    assert len(draft) == 10


async def test_checkout_draft_is_idempotent_when_draft_already_exists():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_relation_type(conn, "t1", relation_type="CUSTOM", example_phrase="x")

    await checkout_draft(conn, "t1")

    draft = await list_relation_types(conn, "t1", status="draft")
    assert len(draft) == 11


async def test_confirm_ontology_promotes_constraints_too():
    conn = await _conn()
    await checkout_draft(conn, "t1")
    await create_term_type(conn, value="客房")
    await create_term_type(conn, value="酒店")
    await add_allowed_combination(conn, "t1", subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店")

    await confirm_ontology(conn, "t1")

    confirmed = await list_allowed_combinations(conn, "t1", status="confirmed")
    assert confirmed == [
        __import__("app.graphrag.ontology_constraints", fromlist=["AllowedCombination"]).AllowedCombination(
            subject_term_type="客房", relation_type="PART_OF", object_term_type="酒店"
        )
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/graphrag/test_ontology_lifecycle.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# app/graphrag/ontology_lifecycle.py
from __future__ import annotations

import aiosqlite

from app.graphrag.ontology_categories import ensure_categories_schema
from app.graphrag.ontology_constraints import ensure_constraints_schema
from app.graphrag.ontology_relations import ensure_relations_schema, seed_default_relation_types

_TABLES_WITH_TENANT_LIFECYCLE = (
    ("tenant_relation_types", ("relation_type",)),
    (
        "term_type_relation_allowlist",
        ("subject_term_type", "relation_type", "object_term_type"),
    ),
)


async def ensure_ontology_schema(conn: aiosqlite.Connection) -> None:
    """统一入口：分类（全局）+ 关系类型/约束（按租户）三张表一起建。分类表虽然不进
    草稿/确认生命周期，但关系类型/约束表的写入校验依赖它已经存在（见
    ontology_constraints.py 的引用校验），放在同一个入口保证调用顺序不会出错。"""
    await ensure_categories_schema(conn)
    await ensure_relations_schema(conn)
    await ensure_constraints_schema(conn)


async def _has_any_row(
    conn: aiosqlite.Connection, table: str, tenant_id: str, status: str
) -> bool:
    cursor = await conn.execute(
        f"SELECT 1 FROM {table} WHERE tenant_id = ? AND status = ? LIMIT 1",
        (tenant_id, status),
    )
    return await cursor.fetchone() is not None


async def checkout_draft(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """检出一份可编辑的草稿：如果该租户已经有草稿，什么都不做（幂等，不覆盖正在
    编辑的内容）；如果没有草稿但有已确认版本，从已确认版本复制一份新草稿；如果
    两者都没有（全新租户），关系类型草稿用 10 种默认值播种，约束表草稿留空
    （没有分类数据支撑，写不出有意义的默认组合）。
    """
    if not await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft"):
        if await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT INTO tenant_relation_types "
                "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
                "source, status) "
                "SELECT tenant_id, relation_type, example_phrase, description, "
                "allow_chain_query, source, 'draft' FROM tenant_relation_types "
                "WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
        else:
            await seed_default_relation_types(conn, tenant_id)
    if not await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "draft"):
        if await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT INTO term_type_relation_allowlist "
                "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
                "SELECT tenant_id, subject_term_type, relation_type, object_term_type, 'draft' "
                "FROM term_type_relation_allowlist WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
    await conn.commit()


async def confirm_ontology(conn: aiosqlite.Connection, tenant_id: str) -> None:
    """把草稿原子性地提升为已确认版本：先删旧的已确认行，再把草稿行的 status 原地
    改成 confirmed——两张表（关系类型、约束）在同一次 commit 里一起提交，不会出现
    "关系类型确认了但约束表没确认"这种半提交状态。确认之后草稿即被清空（status
    改写成 confirmed，不再是 draft），下一次编辑需要重新调用 checkout_draft。
    """
    for table, _ in _TABLES_WITH_TENANT_LIFECYCLE:
        await conn.execute(
            f"DELETE FROM {table} WHERE tenant_id = ? AND status = 'confirmed'", (tenant_id,)
        )
        await conn.execute(
            f"UPDATE {table} SET status = 'confirmed' WHERE tenant_id = ? AND status = 'draft'",
            (tenant_id,),
        )
    await conn.commit()


async def is_ontology_confirmed(conn: aiosqlite.Connection, tenant_id: str) -> bool:
    return await _has_any_row(conn, "tenant_relation_types", tenant_id, "confirmed")
```

- [ ] **Step 4: 运行测试确认全部通过**

Run: `pytest tests/graphrag/test_ontology_lifecycle.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/ontology_lifecycle.py tests/graphrag/test_ontology_lifecycle.py
git commit -m "feat(graphrag): add draft/confirm lifecycle orchestration for ontology"
```

---

### Task 5: 关系类型改名的 Neo4j 边迁移

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Consumes：无新依赖，纯 `Neo4jGraphClient` 新方法。
- Produces：`Neo4jGraphClient.migrate_relation_type_edges(*, tenant_id, old_type, new_type) -> int`
  （返回迁移的边数），供 Task 7 的迁移触发接口调用。

- [ ] **Step 1: 写失败测试**

在 `tests/graphrag/test_neo4j_client.py` 追加（复用文件已有的 `FakeSession`/`FakeDriver`）：

```python
async def test_migrate_relation_type_edges_sends_expected_query():
    session = FakeSession(rows=[{"migrated_count": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.migrate_relation_type_edges(
        tenant_id="t1", old_type="PRECEDES", new_type="COMES_BEFORE"
    )

    assert count == 3
    assert session.last_parameters == {"tenant_id": "t1"}
    assert "MATCH (a)-[r:PRECEDES {tenant_id: $tenant_id}]->(b)" in session.last_query
    assert "CREATE (a)-[r2:COMES_BEFORE]->(b)" in session.last_query


async def test_migrate_relation_type_edges_rejects_invalid_old_type():
    client = Neo4jGraphClient(driver=FakeDriver(FakeSession(rows=[])))

    with pytest.raises(ValueError):
        await client.migrate_relation_type_edges(
            tenant_id="t1", old_type="bad-name", new_type="GOOD_NAME"
        )


async def test_migrate_relation_type_edges_rejects_invalid_new_type():
    client = Neo4jGraphClient(driver=FakeDriver(FakeSession(rows=[])))

    with pytest.raises(ValueError):
        await client.migrate_relation_type_edges(
            tenant_id="t1", old_type="PRECEDES", new_type="bad-name"
        )
```

新类型名字必须满足与旧类型相同的标识符格式校验（`^[A-Z][A-Z0-9_]*$`）——关系类型改名
后的新名字本身也要能安全拼进 Cypher 字符串，不能因为是"改名目标"就豁免这道注入防线，
所以测试用例里的 `new_type` 用 `COMES_BEFORE` 这样的合法标识符，不能用中文这类不满足
格式的名字（业务在确认界面看到的是"关系类型改名"，但底层这个类型名字本身仍然要过第
2 节 `_RELATION_TYPE_PATTERN` 那道格式校验）。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/graphrag/test_neo4j_client.py -k migrate_relation_type -v`
Expected: FAIL（`AttributeError: 'Neo4jGraphClient' object has no attribute
'migrate_relation_type_edges'`）

- [ ] **Step 3: 实现**

在 `app/graphrag/neo4j_client.py` 顶部追加正则常量（复用与 `_ALLOWED_RELATION_TYPES`
同等级别的注入防线校验，不依赖调用方——租户自定义关系类型改名时旧/新类型名都是外部
输入，必须在这里再校验一遍，不能假设上游 `ontology_relations.py` 已经校验过就可以
省略）：

```python
import re

_RELATION_TYPE_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
```

在 `Neo4jGraphClient` 类内追加方法：

```python
    async def migrate_relation_type_edges(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int:
        """把某个租户所有旧类型的边批量改成新类型，返回迁移的边数。

        Neo4j 的关系类型（edge type）一旦写入不可原地修改——"改名"只能新建一条
        新类型的边、把原边的全部属性复制过去、再删掉旧边，这是一次真正的数据
        迁移，不是字符串替换（见 app/graphrag/ontology_relations.py 改名逻辑的
        说明）。这是租户级自定义关系类型改名后，业务显式触发的可选操作——不改名
        的旧边永远留着旧类型字符串也完全可用（query_subgraph 的 1 跳查询不按
        类型过滤），触发这个方法只是为了让图谱里同一语义不再同时存在新旧两种
        类型字符串。

        单条 Cypher 语句一次性处理该租户全部旧类型的边，不做分批——当前没有
        证据支撑单租户单次改名会涉及大量边到需要分批的程度，等真实场景出现
        性能问题再引入分批处理（YAGNI）。
        """
        if not _RELATION_TYPE_NAME_PATTERN.match(old_type):
            raise ValueError(f"旧关系类型名字不合法: {old_type!r}")
        if not _RELATION_TYPE_NAME_PATTERN.match(new_type):
            raise ValueError(f"新关系类型名字不合法: {new_type!r}")
        query = (
            f"MATCH (a)-[r:{old_type} {{tenant_id: $tenant_id}}]->(b) "
            "WITH a, b, r, properties(r) AS props "
            f"CREATE (a)-[r2:{new_type}]->(b) "
            "SET r2 = props "
            "WITH r, r2 "
            "DELETE r "
            "RETURN count(r2) AS migrated_count"
        )
        async with self._driver.session() as session:
            result = await session.run(query, {"tenant_id": tenant_id})
            rows = await result.data()
        return rows[0]["migrated_count"] if rows else 0
```

- [ ] **Step 4: 运行测试确认全部通过**

Run: `pytest tests/graphrag/test_neo4j_client.py -k migrate_relation_type -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(graphrag): add opt-in Neo4j edge migration for relation-type renames"
```

---

### Task 6: 术语表接入分类硬约束 + 属性字段

**Files:**
- Modify: `app/graphrag/ontology.py`
- Modify: `app/graphrag/terms_store.py`
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `app/api/admin_terms_routes.py`
- Test: `tests/graphrag/test_terms_store.py`
- Test: `tests/graphrag/test_neo4j_client.py`
- Test: `tests/api/test_admin_terms_routes.py`

**Interfaces:**
- Consumes：Task 1 的 `ontology_categories.list_term_types`/`list_product_lines`。
- Produces：`Term.extra_properties: dict[str, str]`（新字段，默认空字典）；
  `create_term`/`update_term` 新增 `extra_properties` 参数并做校验；`sync_term()` 把
  `extra_properties` 写进 Neo4j 节点属性。

- [ ] **Step 1: 写失败测试——`Term` 新增字段**

修改 `tests/graphrag/test_terms_store.py`（追加，不改动已有测试）：

```python
async def test_create_term_persists_extra_properties():
    conn = await _conn()
    await ensure_categories_schema(conn)
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])
    await create_product_line(conn, value="示例产品线")

    await create_term(
        conn,
        standard_name="错误码E502",
        aliases=[],
        term_type="错误码",
        product_line="示例产品线",
        extra_properties={"严重等级": "高"},
    )

    term = await get_term(conn, "错误码E502")
    assert term.extra_properties == {"严重等级": "高"}


async def test_create_term_rejects_unknown_term_type():
    conn = await _conn()
    await ensure_categories_schema(conn)
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, standard_name="x", aliases=[], term_type="没有这个分类",
            product_line="示例产品线",
        )


async def test_create_term_rejects_unknown_product_line():
    conn = await _conn()
    await ensure_categories_schema(conn)
    await create_term_type(conn, value="错误码")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, standard_name="x", aliases=[], term_type="错误码",
            product_line="没有这个产品线",
        )


async def test_create_term_rejects_extra_property_not_declared_on_term_type():
    conn = await _conn()
    await ensure_categories_schema(conn)
    await create_term_type(conn, value="错误码", extra_fields=["严重等级"])
    await create_product_line(conn, value="示例产品线")

    with pytest.raises(UnknownCategoryError):
        await create_term(
            conn, standard_name="x", aliases=[], term_type="错误码",
            product_line="示例产品线", extra_properties={"没声明过的字段": "值"},
        )
```

需要在文件顶部补充 import：
```python
from app.graphrag.ontology_categories import create_term_type, create_product_line, ensure_categories_schema
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/graphrag/test_terms_store.py -v`
Expected: 新增测试 FAIL（`create_term` 还不接受 `extra_properties` 参数，也不做分类
校验）

- [ ] **Step 3: 扩展 `Term` dataclass**

修改 `app/graphrag/ontology.py`：

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Term:
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str] = field(default_factory=dict)
```

`load_terminology()` 函数体不需要改（YAML 里没有 `extra_properties` 字段时，构造
`Term(...)` 不传这个参数会自动用默认的空字典，向后兼容）。

- [ ] **Step 4: 扩展 `terms` 表结构与 `terms_store.py` 校验逻辑**

修改 `app/graphrag/terms_store.py`：

```python
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from app.db_migrations import add_column_if_missing
from app.graphrag.ontology import Term, load_terminology
from app.graphrag.ontology_categories import (
    ensure_categories_schema,
    list_product_lines,
    list_term_types,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    standard_name TEXT PRIMARY KEY,
    aliases TEXT NOT NULL,
    term_type TEXT NOT NULL,
    product_line TEXT NOT NULL
);
"""


class TermNotFoundError(Exception):
    """指定的 standard_name 在术语表里不存在。"""


class TermNameConflictError(Exception):
    """提交的 standard_name 或某个 alias，跟另一个已存在的术语的
    standard_name/alias 重复。"""


class UnknownCategoryError(Exception):
    """提交的 term_type/product_line 不在全局分类枚举表里，或 extra_properties
    里出现了该 term_type 没有声明过的字段名——本体 schema 基座计划把这两项从
    "自由文本、无校验" 收紧成硬约束，理由见
    docs/superpowers/specs/2026-08-14-ontology-schema-design.md 第 3 节。"""


async def ensure_terms_schema(
    conn: aiosqlite.Connection, *, seed_yaml_path: Path | None = None
) -> None:
    """幂等建表，并确保分类枚举表存在（term_type/product_line 硬约束依赖它）。

    向后兼容桥接：如果分类枚举表是空的、但 terms 表已经有历史数据（老版本上线
    时term_type/product_line 还是自由文本，没有枚举表），自动把历史数据里出现过
    的去重值导入枚举表——否则硬约束上线的第一刻，任何现有术语的编辑请求都会
    因为找不到匹配的枚举值报错，这不是假设性风险，terminology_seed.yaml 的两条
    占位数据（error_code/module）就是真实会撞上这个问题的例子。只在枚举表为空
    时执行一次，不会覆盖业务后续的正常编辑。
    """
    await ensure_categories_schema(conn)
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    )
    table_already_existed = await cursor.fetchone() is not None
    await conn.executescript(_SCHEMA_SQL)
    await add_column_if_missing(
        conn, table="terms", column="extra_properties", ddl="TEXT NOT NULL DEFAULT '{}'"
    )
    await conn.commit()
    if not table_already_existed and seed_yaml_path is not None and seed_yaml_path.exists():
        for term in load_terminology(seed_yaml_path):
            await conn.execute(
                "INSERT OR IGNORE INTO terms "
                "(standard_name, aliases, term_type, product_line, extra_properties) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    term.standard_name,
                    json.dumps(term.aliases, ensure_ascii=False),
                    term.term_type,
                    term.product_line,
                    json.dumps(term.extra_properties, ensure_ascii=False),
                ),
            )
        await conn.commit()
    await _bridge_seed_categories_from_existing_terms(conn)


async def _bridge_seed_categories_from_existing_terms(conn: aiosqlite.Connection) -> None:
    known_types = await list_term_types(conn)
    known_lines = await list_product_lines(conn)
    if known_types or known_lines:
        return
    cursor = await conn.execute("SELECT DISTINCT term_type FROM terms")
    distinct_types = [row[0] for row in await cursor.fetchall()]
    cursor = await conn.execute("SELECT DISTINCT product_line FROM terms")
    distinct_lines = [row[0] for row in await cursor.fetchall()]
    if not distinct_types and not distinct_lines:
        return
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types (value, extra_fields) VALUES (?, '[]')",
            (value,),
        )
    for value in distinct_lines:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_product_lines (value) VALUES (?)", (value,)
        )
    await conn.commit()


def _row_to_term(row: aiosqlite.Row) -> Term:
    return Term(
        standard_name=row["standard_name"],
        aliases=json.loads(row["aliases"]),
        term_type=row["term_type"],
        product_line=row["product_line"],
        extra_properties=json.loads(row["extra_properties"]),
    )


async def list_terms(conn: aiosqlite.Connection) -> list[Term]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT standard_name, aliases, term_type, product_line, extra_properties "
        "FROM terms ORDER BY standard_name"
    )
    rows = await cursor.fetchall()
    return [_row_to_term(row) for row in rows]


async def get_term(conn: aiosqlite.Connection, standard_name: str) -> Term:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT standard_name, aliases, term_type, product_line, extra_properties "
        "FROM terms WHERE standard_name = ?",
        (standard_name,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise TermNotFoundError(f"术语不存在: {standard_name}")
    return _row_to_term(row)


async def _validate_categories(
    conn: aiosqlite.Connection, *, term_type: str, product_line: str, extra_properties: dict[str, str]
) -> None:
    types = await list_term_types(conn)
    types_by_value = {t.value: t for t in types}
    if term_type not in types_by_value:
        raise UnknownCategoryError(f"未知分类: {term_type!r}")
    if product_line not in await list_product_lines(conn):
        raise UnknownCategoryError(f"未知产品线: {product_line!r}")
    declared_fields = set(types_by_value[term_type].extra_fields)
    unknown = set(extra_properties) - declared_fields
    if unknown:
        raise UnknownCategoryError(
            f"分类 {term_type!r} 没有声明这些属性字段: {sorted(unknown)}"
        )


async def _check_name_conflict(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    aliases: list[str],
    exclude_standard_name: str | None = None,
) -> None:
    all_terms = await list_terms(conn)
    candidate_names = {standard_name, *aliases}
    for term in all_terms:
        if term.standard_name == exclude_standard_name:
            continue
        existing_names = {term.standard_name, *term.aliases}
        overlap = candidate_names & existing_names
        if overlap:
            conflicting = next(iter(overlap))
            raise TermNameConflictError(
                f"{conflicting!r} 已经是术语 {term.standard_name!r} 的别名/标准名，不能重复使用"
            )


async def create_term(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, str] | None = None,
) -> None:
    extra_properties = extra_properties or {}
    await _validate_categories(
        conn, term_type=term_type, product_line=product_line, extra_properties=extra_properties
    )
    await _check_name_conflict(conn, standard_name=standard_name, aliases=aliases)
    try:
        await conn.execute(
            "INSERT INTO terms (standard_name, aliases, term_type, product_line, extra_properties) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{standard_name!r} 已经是已有术语的标准名，不能重复创建")
    await conn.commit()


async def update_term(
    conn: aiosqlite.Connection,
    *,
    standard_name: str,
    new_standard_name: str,
    aliases: list[str],
    term_type: str,
    product_line: str,
    extra_properties: dict[str, str] | None = None,
) -> None:
    extra_properties = extra_properties or {}
    await get_term(conn, standard_name)
    await _validate_categories(
        conn, term_type=term_type, product_line=product_line, extra_properties=extra_properties
    )
    await _check_name_conflict(
        conn, standard_name=new_standard_name, aliases=aliases,
        exclude_standard_name=standard_name,
    )
    try:
        await conn.execute(
            "UPDATE terms SET standard_name=?, aliases=?, term_type=?, product_line=?, "
            "extra_properties=? WHERE standard_name=?",
            (
                new_standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                product_line,
                json.dumps(extra_properties, ensure_ascii=False),
                standard_name,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{new_standard_name!r} 已经是已有术语的标准名，不能重复使用")
    await conn.commit()


async def delete_term(conn: aiosqlite.Connection, standard_name: str) -> None:
    await get_term(conn, standard_name)
    await conn.execute("DELETE FROM terms WHERE standard_name=?", (standard_name,))
    await conn.commit()
```

- [ ] **Step 5: 运行测试确认全部通过**

Run: `pytest tests/graphrag/test_terms_store.py -v`
Expected: 全部 PASS（含既有测试，`create_term`/`update_term` 现在要求调用方先建好
分类枚举，检查所有既有测试是否已经在 fixture 里创建了对应的 term_type/product_line
——如果没有，按下面 Step 5b 修）

- [ ] **Step 5b: 修既有测试的 fixture**

`test_terms_store.py` 里所有既有测试（round-1 计划已写的那些）如果直接用
`term_type="错误码", product_line="示例产品线"` 这类字面量、却没有先调用
`create_term_type`/`create_product_line`，现在会因为 `UnknownCategoryError` 失败——
在测试文件的公共 fixture/辅助函数里补上：

```python
async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)  # 内部已经调用 ensure_categories_schema
    await create_term_type(conn, value="错误码")
    await create_term_type(conn, value="模块")
    await create_product_line(conn, value="示例产品线")
    return conn
```

（具体改法取决于既有 fixture 的实际写法，实现时先跑一遍测试看哪些失败，针对性地在
对应 fixture 里补分类，不要不加区分地全局改。）

- [ ] **Step 5c: 写失败测试——移除属性字段不清除已有术语的旧值（孤点保护）**

这条测试直接对应 spec 文档第 7 节"孤点数据保护规则"表格里"移除属性字段"那一行——
`update_term_type` 只改 `ontology_term_types.extra_fields` 这个声明，不会、也不应该
反过来清理 `terms.extra_properties` 里已经写的值，这里用测试把这个保证钉住，不能只
靠"代码里没写级联"这种隐式正确性。

```python
async def test_removing_extra_field_from_term_type_preserves_existing_term_value():
    conn = await _conn()
    await create_term_type(conn, value="错误码", extra_fields=["严重等级", "影响范围"])
    await create_product_line(conn, value="示例产品线")
    await create_term(
        conn, standard_name="错误码E502", aliases=[], term_type="错误码",
        product_line="示例产品线", extra_properties={"严重等级": "高", "影响范围": "全站不可用"},
    )

    await update_term_type(conn, value="错误码", new_value="错误码", extra_fields=["严重等级"])

    term = await get_term(conn, "错误码E502")
    assert term.extra_properties == {"严重等级": "高", "影响范围": "全站不可用"}
```

- [ ] **Step 5d: 运行测试确认通过（预期本来就通过，无需改动实现）**

Run: `pytest tests/graphrag/test_terms_store.py -k preserves_existing_term_value -v`
Expected: PASS——`update_term_type` 的实现（Task 1）本来就没有触碰 `terms` 表的
`extra_properties` 列，这条测试是确认这个"没做的事"符合预期，不是驱动新代码。如果
这条测试失败，说明 Task 1 或本任务的某处实现意外引入了级联清理逻辑，需要回头检查。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/ontology.py app/graphrag/terms_store.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): enforce category hard constraints and add extra_properties to terms"
```

- [ ] **Step 7: 写失败测试——`sync_term()` 携带 extra_properties**

修改 `tests/graphrag/test_neo4j_client.py`（找到既有的 `sync_term` 测试并追加一个新
用例，不改动已有的）：

```python
async def test_sync_term_writes_extra_properties():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        standard_name="错误码E502", aliases=[], term_type="错误码",
        product_line="示例产品线", extra_properties={"严重等级": "高"},
    )

    await client.sync_term(term)

    assert session.last_parameters["extra_properties"] == {"严重等级": "高"}
    assert "SET t += $extra_properties" in session.last_query
```

- [ ] **Step 8: 运行测试确认失败**

Run: `pytest tests/graphrag/test_neo4j_client.py -k sync_term_writes_extra -v`
Expected: FAIL

- [ ] **Step 9: 实现**

修改 `app/graphrag/neo4j_client.py`：

```python
_SYNC_TERM_QUERY = """
MERGE (t:Term {standard_name: $standard_name})
SET t.type = $type, t.product_line = $product_line
SET t += $extra_properties
WITH t
UNWIND $aliases AS alias_name
MERGE (a:Term {alias_name: alias_name})
MERGE (a)-[:ALIAS_OF]->(t)
"""
```

`sync_term()` 方法体里传参新增 `"extra_properties": term.extra_properties`（找到现有
`session.run(_SYNC_TERM_QUERY, {...})` 调用处的参数字典追加这一项）。`SET t +=
$extra_properties` 是 Neo4j 的 map 合并写法，只更新/新增 map 里出现的键，不清空节点
上其他既有属性——属性字段本身不需要标识符格式校验（不同于关系类型，Neo4j 属性键名
可以通过参数化的 map 写入，不需要拼进 Cypher 字符串，不存在注入风险，见 spec 文档
第 5.3 节）。

- [ ] **Step 10: 运行测试确认全部通过**

Run: `pytest tests/graphrag/test_neo4j_client.py -v`
Expected: 全部 PASS

- [ ] **Step 11: 提交**

```bash
git add app/graphrag/neo4j_client.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(graphrag): sync term extra_properties into Neo4j via parameterized map"
```

- [ ] **Step 12: 接入 `admin_terms_routes.py`**

修改 `app/api/admin_terms_routes.py`：`TermResponse`/`TermWriteRequest` 都新增
`extra_properties: dict[str, str] = {}` 字段；`_to_response`、`create_new_term`、
`update_existing_term` 里构造 `Term(...)` 和调用 `create_term`/`update_term` 的地方
都要把 `extra_properties` 传过去；`create_new_term`/`update_existing_term` 现有的
`except TermNameConflictError`/`except TermNotFoundError` 分支旁边新增：

```python
    except UnknownCategoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

（`UnknownCategoryError` 从 `app.graphrag.terms_store` 导入，追加到现有 import 列表。）

新增测试到 `tests/api/test_admin_terms_routes.py`（在既有 fixture 里补
`create_term_type`/`create_product_line`，参照 Step 5b 的方式）：

```python
async def test_create_term_with_unknown_category_returns_400():
    ...  # 复用文件里既有的 client/override 模式，POST 一个不存在的 term_type，
         # 断言 response.status_code == 400
```

- [ ] **Step 13: 运行测试确认全部通过**

Run: `pytest tests/api/test_admin_terms_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 14: 提交**

```bash
git add app/api/admin_terms_routes.py tests/api/test_admin_terms_routes.py
git commit -m "feat(api): expose extra_properties and category errors on term CRUD routes"
```

---

### Task 7: 本体管理后台 API 路由

**Files:**
- Create: `app/api/admin_ontology_routes.py`
- Modify: `app/main.py`
- Test: `tests/api/test_admin_ontology_routes.py`

**Interfaces:**
- Consumes: Task 1-6 的全部 store 函数。
- Produces：`app/api/admin_ontology_routes.py::router`（`APIRouter`），注册进
  `app/main.py`。

- [ ] **Step 1: 写失败测试——分类路由（立即生效）**

```python
# tests/api/test_admin_ontology_routes.py
from __future__ import annotations

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.main import app

pytestmark = pytest.mark.anyio


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    return conn


@pytest.fixture
def client(monkeypatch):
    conn_holder: dict[str, aiosqlite.Connection] = {}

    async def _get_conn():
        if "conn" not in conn_holder:
            conn_holder["conn"] = await _review_conn()
        return conn_holder["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_and_list_term_types(client):
    resp = client.post(
        "/api/admin/ontology/term-types",
        json={"value": "错误码", "extra_fields": ["严重等级"]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/admin/ontology/term-types", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    assert resp.json() == {"term_types": [{"value": "错误码", "extra_fields": ["严重等级"]}]}


def test_delete_term_type_in_use_returns_409(client):
    client.post(
        "/api/admin/ontology/term-types", json={"value": "错误码", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/product-lines", json={"value": "示例产品线"},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/terms",
        json={"standard_name": "x", "aliases": [], "term_type": "错误码", "product_line": "示例产品线"},
        headers={"Authorization": "Bearer x"},
    )

    resp = client.delete("/api/admin/ontology/term-types/错误码", headers={"Authorization": "Bearer x"})

    assert resp.status_code == 409
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/api/test_admin_ontology_routes.py -v`
Expected: FAIL（`404`，路由不存在）

- [ ] **Step 3: 实现分类路由**

```python
# app/api/admin_ontology_routes.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import aiosqlite

from app.api import deps
from app.graphrag.ontology_categories import (
    CategoryInUseError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    create_product_line,
    create_term_type,
    delete_product_line,
    delete_term_type,
    list_product_lines,
    list_term_types,
    update_product_line,
    update_term_type,
)
from app.graphrag.ontology_constraints import (
    UnknownCategoryError as ConstraintUnknownCategoryError,
    UnknownRelationTypeError,
    add_allowed_combination,
    list_allowed_combinations,
    remove_allowed_combination,
)
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, is_ontology_confirmed
from app.graphrag.ontology_relations import (
    InvalidRelationTypeNameError,
    RelationTypeNotFoundError,
    create_relation_type,
    delete_relation_type,
    list_relation_types,
    update_relation_type,
)
from app.graphrag.neo4j_client import Neo4jGraphClient

router = APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)])


class TermTypeWriteRequest(BaseModel):
    value: str
    extra_fields: list[str] = []


class ProductLineWriteRequest(BaseModel):
    value: str


@router.get("/term-types")
async def list_term_type_categories(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn)
    return {"term_types": [{"value": t.value, "extra_fields": t.extra_fields} for t in result]}


@router.post("/term-types")
async def create_term_type_category(
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await create_term_type(review_conn, value=payload.value, extra_fields=payload.extra_fields)
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value, "extra_fields": payload.extra_fields}


@router.put("/term-types/{value}")
async def update_term_type_category(
    value: str,
    payload: TermTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await update_term_type(
            review_conn, value=value, new_value=payload.value, extra_fields=payload.extra_fields
        )
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="分类不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value, "extra_fields": payload.extra_fields}


@router.delete("/term-types/{value}")
async def delete_term_type_category(
    value: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    try:
        await delete_term_type(review_conn, value)
    except CategoryInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"deleted": True}


@router.get("/product-lines")
async def list_product_line_categories(
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    return {"product_lines": await list_product_lines(review_conn)}


@router.post("/product-lines")
async def create_product_line_category(
    payload: ProductLineWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await create_product_line(review_conn, value=payload.value)
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value}


@router.put("/product-lines/{value}")
async def update_product_line_category(
    value: str,
    payload: ProductLineWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await update_product_line(review_conn, value=value, new_value=payload.value)
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail="产品线不存在")
    except CategoryNameConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"value": payload.value}


@router.delete("/product-lines/{value}")
async def delete_product_line_category(
    value: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    try:
        await delete_product_line(review_conn, value)
    except CategoryInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"deleted": True}
```

- [ ] **Step 4: 运行测试确认分类路由通过**

Run: `pytest tests/api/test_admin_ontology_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/api/admin_ontology_routes.py tests/api/test_admin_ontology_routes.py
git commit -m "feat(api): add global category admin routes"
```

- [ ] **Step 6: 写失败测试——关系类型/约束/生命周期路由**

```python
def test_checkout_confirm_and_list_relation_types(client):
    resp = client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/relation-types?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert len(resp.json()["relation_types"]) == 10

    resp = client.post("/api/admin/ontology/t1/confirm", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/relation-types?status=confirmed", headers={"Authorization": "Bearer x"}
    )
    assert len(resp.json()["relation_types"]) == 10


def test_create_relation_type_rejects_bad_name(client):
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.post(
        "/api/admin/ontology/t1/relation-types",
        json={"relation_type": "bad-name", "example_phrase": "x"},
        headers={"Authorization": "Bearer x"},
    )

    assert resp.status_code == 400


def test_add_and_list_constraints(client):
    client.post(
        "/api/admin/ontology/term-types", json={"value": "客房", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post(
        "/api/admin/ontology/term-types", json={"value": "酒店", "extra_fields": []},
        headers={"Authorization": "Bearer x"},
    )
    client.post("/api/admin/ontology/t1/checkout", headers={"Authorization": "Bearer x"})

    resp = client.post(
        "/api/admin/ontology/t1/constraints",
        json={"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    resp = client.get(
        "/api/admin/ontology/t1/constraints?status=draft", headers={"Authorization": "Bearer x"}
    )
    assert resp.json()["constraints"] == [
        {"subject_term_type": "客房", "relation_type": "PART_OF", "object_term_type": "酒店"}
    ]
```

- [ ] **Step 7: 运行测试确认失败**

Run: `pytest tests/api/test_admin_ontology_routes.py -v`
Expected: 新增测试 FAIL（404）

- [ ] **Step 8: 实现关系类型/约束/生命周期路由**

在 `app/api/admin_ontology_routes.py` 追加：

```python
class RelationTypeWriteRequest(BaseModel):
    relation_type: str
    example_phrase: str
    description: str = ""
    allow_chain_query: bool = False


class ConstraintWriteRequest(BaseModel):
    subject_term_type: str
    relation_type: str
    object_term_type: str


class MigrateRelationTypeRequest(BaseModel):
    old_type: str
    new_type: str


def _relation_type_to_dict(item) -> dict:
    return {
        "relation_type": item.relation_type,
        "example_phrase": item.example_phrase,
        "description": item.description,
        "allow_chain_query": item.allow_chain_query,
        "source": item.source,
    }


@router.get("/{tenant_id}/relation-types")
async def list_tenant_relation_types(
    tenant_id: str, status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_relation_types(review_conn, tenant_id, status=status)
    return {"relation_types": [_relation_type_to_dict(r) for r in result]}


@router.post("/{tenant_id}/relation-types")
async def create_tenant_relation_type(
    tenant_id: str, payload: RelationTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await create_relation_type(
            review_conn, tenant_id, relation_type=payload.relation_type,
            example_phrase=payload.example_phrase, description=payload.description,
            allow_chain_query=payload.allow_chain_query,
        )
    except InvalidRelationTypeNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.put("/{tenant_id}/relation-types/{relation_type}")
async def update_tenant_relation_type(
    tenant_id: str, relation_type: str, payload: RelationTypeWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await update_relation_type(
            review_conn, tenant_id, relation_type=relation_type,
            example_phrase=payload.example_phrase, description=payload.description,
            allow_chain_query=payload.allow_chain_query,
        )
    except InvalidRelationTypeNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RelationTypeNotFoundError:
        raise HTTPException(status_code=404, detail="关系类型不存在")
    return payload.model_dump()


@router.delete("/{tenant_id}/relation-types/{relation_type}")
async def delete_tenant_relation_type(
    tenant_id: str, relation_type: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await delete_relation_type(review_conn, tenant_id, relation_type)
    return {"deleted": True}


@router.post("/{tenant_id}/relation-types/migrate")
async def migrate_tenant_relation_type(
    tenant_id: str, payload: MigrateRelationTypeRequest,
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> dict:
    try:
        count = await graph_client.migrate_relation_type_edges(
            tenant_id=tenant_id, old_type=payload.old_type, new_type=payload.new_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"migrated_count": count}


@router.get("/{tenant_id}/constraints")
async def list_tenant_constraints(
    tenant_id: str, status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_allowed_combinations(review_conn, tenant_id, status=status)
    return {
        "constraints": [
            {
                "subject_term_type": c.subject_term_type,
                "relation_type": c.relation_type,
                "object_term_type": c.object_term_type,
            }
            for c in result
        ]
    }


@router.post("/{tenant_id}/constraints")
async def add_tenant_constraint(
    tenant_id: str, payload: ConstraintWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    try:
        await add_allowed_combination(
            review_conn, tenant_id, subject_term_type=payload.subject_term_type,
            relation_type=payload.relation_type, object_term_type=payload.object_term_type,
        )
    except (ConstraintUnknownCategoryError, UnknownRelationTypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return payload.model_dump()


@router.delete("/{tenant_id}/constraints")
async def remove_tenant_constraint(
    tenant_id: str, payload: ConstraintWriteRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await remove_allowed_combination(
        review_conn, tenant_id, subject_term_type=payload.subject_term_type,
        relation_type=payload.relation_type, object_term_type=payload.object_term_type,
    )
    return {"deleted": True}


@router.post("/{tenant_id}/checkout")
async def checkout_tenant_ontology_draft(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await checkout_draft(review_conn, tenant_id)
    return {"checked_out": True}


@router.post("/{tenant_id}/confirm")
async def confirm_tenant_ontology(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    await confirm_ontology(review_conn, tenant_id)
    return {"confirmed": True}


@router.get("/{tenant_id}/status")
async def get_tenant_ontology_status(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    return {"confirmed": await is_ontology_confirmed(review_conn, tenant_id)}
```

删除路由用 `DELETE` 带 body（`ConstraintWriteRequest`）不是 REST 惯例最佳实践，但约束
的自然主键是三元组，放进路径会有转义/可读性问题（`subject/relation/object` 三段路径
参数），这里选择用请求体传三元组，是 YAGNI 下最简单能工作的方案，不是最终定案——如果
后续图形界面开发时发现这个设计不好用（比如浏览器 fetch 对 DELETE 带 body 的支持不
一致），到时候再改成 POST 到一个 `/constraints/remove` 端点。

- [ ] **Step 9: 注册路由**

修改 `app/main.py`：导入 `admin_ontology_router`（`from app.api.admin_ontology_routes
import router as admin_ontology_router`），`app.include_router(admin_ontology_router)`
（放在其他 `admin_*_router` 注册语句旁边）。

- [ ] **Step 10: 运行测试确认全部通过**

Run: `pytest tests/api/test_admin_ontology_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 11: 运行全量测试套件确认无回归**

Run: `pytest -q`
Expected: 除已知的 `test_returns_none_when_tts_not_configured`（预先存在、无关此次
改动）外全部通过

- [ ] **Step 12: 提交**

```bash
git add app/api/admin_ontology_routes.py app/main.py tests/api/test_admin_ontology_routes.py
git commit -m "feat(api): add tenant relation-type/constraint/lifecycle admin routes"
```
