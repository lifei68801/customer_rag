# schema_etl.py ETL 写入引擎 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现按已确认 schema + 列映射 YAML 配置，把结构化 CSV 源数据（如 MUJI 的商品目录）确定性写入 Term/Neo4j 双存储的 ETL 引擎，包含稳定码注册机制，为结构化数据租户接入打好基础设施。

**Architecture:** 五个独立模块串起来：稳定码注册表（`etl_stable_code_registry.py`）、新增的 `terms_store.py` upsert 写入接口、列映射配置解析（`schema_etl_config.py`）、node_key/类型转换的行处理逻辑（`schema_etl_row_processing.py`），最后是编排整个流程的写入引擎主体（`schema_etl.py`，含 CLI 入口）。全程只读 CSV 文件，不直连外部数据库；只做 MERGE/upsert，不做旧数据自动清理。

**Tech Stack:** Python 3.12（标准库 `csv` 模块，不引入新依赖）、aiosqlite、PyYAML（项目已有依赖，`terminology_seed.yaml` 已在用）、pytest + pytest-asyncio（`anyio` 标记）。

**Spec:** `docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md`，关联 `docs/superpowers/specs/2026-08-15-etl-driven-schema-construction-design.md`（第 7 节）。

## Global Constraints

- 本计划只实现 **CSV** 格式的源文件读取（Python 标准库 `csv` 模块）。JSON/Parquet 是 spec 第 1 节列出的可选形态，但没有真实需求驱动现在就实现，留给未来单独评估——不在本计划范围内。
- `number[]` 类型的字段在 CSV 单元格里用 `;` 分隔多个数字（如 `"20.5;10.0"`）——逗号已经是 CSV 本身的列分隔符，不能复用。
- `node_key` 的最终值：各 `node_key_parts` 解析出的值按英文冒号 `:` 拼接，前面加 `{term_type}:` 前缀（如 `Product:{product_group_id}`、`Variant:{dim_code}:{value_code}`）。
- 所有新增函数的 `tenant_id` 参数一律是必填关键字参数，不给默认值——与本项目既有的 `terms_store.py`/`ontology_categories.py` 约定一致。
- 稳定码分配（`etl_stable_code_registry`）假设同一租户的 ETL 任务串行执行，不做并发锁——spec 第 3.3 节已论证的 YAGNI 决定，不需要在本计划里重新实现。
- ETL 写入只做 MERGE/upsert，never 删除已有的节点或边——spec 第 6.5 节已论证。
- 单行数据出错（列不存在、类型转换失败、`standard_name` 唯一性冲突）跳过该行、记录日志，不中断整批处理——spec 第 6.4 节。
- ETL 运行前必须检查 `is_ontology_confirmed(conn, tenant_id)` 为真，否则拒绝运行——spec 第 6.2 节。

---

### Task 1: 稳定码注册表模块

**Files:**
- Create: `app/graphrag/etl_stable_code_registry.py`
- Test: `tests/graphrag/test_etl_stable_code_registry.py`

**Interfaces:**
- Produces：
  - `async def ensure_stable_code_registry_schema(conn: aiosqlite.Connection) -> None`
  - `async def allocate_stable_code(conn: aiosqlite.Connection, *, tenant_id: str, scope: str, raw_value: str) -> str`（已分配过就复用，没有就分配新的，返回纯数字序号字符串如 `"00001"`）

- [ ] **Step 1: 写失败的测试**

```python
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.etl_stable_code_registry import (
    allocate_stable_code,
    ensure_stable_code_registry_schema,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)
    return conn


async def test_allocate_stable_code_first_time_assigns_00001():
    conn = await _conn()
    code = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    assert code == "00001"


async def test_allocate_stable_code_reuses_existing_code_for_same_raw_value():
    conn = await _conn()
    first = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    second = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    assert first == second == "00001"


async def test_allocate_stable_code_increments_within_same_scope():
    conn = await _conn()
    await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="抹茶")
    code = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="草莓")
    assert code == "00002"


async def test_allocate_stable_code_scopes_independently():
    """不同 scope（如不同维度）下相同原始值应该各自分配独立编号，
    不共享同一套计数——见 spec 第 3.1 节 scope 的作用。"""
    conn = await _conn()
    code_a = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_007", raw_value="黑色")
    code_b = await allocate_stable_code(conn, tenant_id="muji", scope="VariantValue:dim_008", raw_value="黑色")
    assert code_a == "00001"
    assert code_b == "00001"


async def test_allocate_stable_code_scopes_by_tenant():
    conn = await _conn()
    code_a = await allocate_stable_code(conn, tenant_id="tenant_a", scope="VariantValue:dim_007", raw_value="抹茶")
    code_b = await allocate_stable_code(conn, tenant_id="tenant_b", scope="VariantValue:dim_007", raw_value="抹茶")
    assert code_a == "00001"
    assert code_b == "00001"


async def test_ensure_stable_code_registry_schema_is_idempotent():
    conn = await _conn()
    await allocate_stable_code(conn, tenant_id="muji", scope="s", raw_value="v")
    await ensure_stable_code_registry_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    code = await allocate_stable_code(conn, tenant_id="muji", scope="s", raw_value="v")
    assert code == "00001"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_etl_stable_code_registry.py -v`
Expected: FAIL——模块不存在

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etl_stable_code_registry (
    tenant_id    TEXT NOT NULL,
    scope        TEXT NOT NULL,
    raw_value    TEXT NOT NULL,
    stable_code  TEXT NOT NULL,
    allocated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, scope, raw_value)
);
"""


async def ensure_stable_code_registry_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def allocate_stable_code(
    conn: aiosqlite.Connection, *, tenant_id: str, scope: str, raw_value: str
) -> str:
    """给定 (tenant_id, scope, raw_value)，命中已有分配就复用，未命中就在该
    scope 下分配一个新的五位数序号（从 "00001" 开始）。假设同一租户的 ETL
    任务串行执行，查询命中判断与插入之间没有加锁——见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 3.3 节
    的并发假设说明。
    """
    cursor = await conn.execute(
        "SELECT stable_code FROM etl_stable_code_registry "
        "WHERE tenant_id = ? AND scope = ? AND raw_value = ?",
        (tenant_id, scope, raw_value),
    )
    row = await cursor.fetchone()
    if row is not None:
        return row[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM etl_stable_code_registry WHERE tenant_id = ? AND scope = ?",
        (tenant_id, scope),
    )
    count = (await cursor.fetchone())[0]
    stable_code = f"{count + 1:05d}"
    await conn.execute(
        "INSERT INTO etl_stable_code_registry (tenant_id, scope, raw_value, stable_code, allocated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (tenant_id, scope, raw_value, stable_code),
    )
    await conn.commit()
    return stable_code
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_etl_stable_code_registry.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/etl_stable_code_registry.py tests/graphrag/test_etl_stable_code_registry.py
git commit -m "feat(graphrag): add ETL stable-code registry for scope-based value identity"
```

---

### Task 2: terms_store.py 新增 upsert_term_with_node_key

**Files:**
- Modify: `app/graphrag/terms_store.py`
- Test: `tests/graphrag/test_terms_store.py`

**Interfaces:**
- Consumes：`_validate_categories`（terms_store.py 已有）、`TermNameConflictError`（terms_store.py 已有）
- Produces：
  - `async def upsert_term_with_node_key(conn, *, tenant_id: str, node_key: str, standard_name: str, aliases: list[str], term_type: str, product_line: str, extra_properties: dict[str, object] | None = None) -> None`

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_terms_store.py` 新增：

```python
async def test_upsert_term_with_node_key_creates_new_row():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import create_term_type, create_product_line
    await create_term_type(conn, tenant_id="muji", value="Product")
    await create_product_line(conn, value="MUJI")

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term.node_key == "Product:1001"


async def test_upsert_term_with_node_key_updates_existing_row_by_node_key():
    """再次 upsert 同一个 node_key、standard_name 变了——更新而不是报冲突，
    这是 upsert 和 create_term 的本质区别（见 terms_store.py 里的说明）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import create_term_type, create_product_line
    await create_term_type(conn, tenant_id="muji", value="Product")
    await create_product_line(conn, value="MUJI")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒(新装)",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    all_terms = await list_terms(conn, tenant_id="muji")
    assert len(all_terms) == 1
    assert all_terms[0].standard_name == "圆角收纳盒(新装)"
    assert all_terms[0].node_key == "Product:1001"


async def test_upsert_term_with_node_key_rejects_duplicate_standard_name_different_node_key():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import create_term_type, create_product_line
    await create_term_type(conn, tenant_id="muji", value="Product")
    await create_product_line(conn, value="MUJI")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Product:1001", standard_name="圆角收纳盒",
        aliases=[], term_type="Product", product_line="MUJI",
    )

    with pytest.raises(TermNameConflictError):
        await upsert_term_with_node_key(
            conn, tenant_id="muji", node_key="Product:1002", standard_name="圆角收纳盒",
            aliases=[], term_type="Product", product_line="MUJI",
        )


async def test_upsert_term_with_node_key_typed_extra_properties():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import ExtraFieldSpec, create_term_type, create_product_line
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await create_product_line(conn, value="MUJI")

    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue", product_line="MUJI",
        extra_properties={"numeric_value": 70},
    )

    term = await get_term(conn, tenant_id="muji", standard_name="抹茶")
    assert term.extra_properties == {"numeric_value": 70}


async def test_upsert_term_with_node_key_grandfathers_removed_field_on_re_upsert():
    """字段被从 term_type 移除后，upsert 同一个 node_key 时旧值也要能豁免类型/
    未知字段校验——延续 update_term 已有的豁免原则。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    from app.graphrag.ontology_categories import (
        ExtraFieldSpec, create_term_type, create_product_line, update_term_type,
    )
    await create_term_type(
        conn, tenant_id="muji", value="VariantValue",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )
    await create_product_line(conn, value="MUJI")
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue", product_line="MUJI",
        extra_properties={"numeric_value": 70},
    )
    await update_term_type(
        conn, tenant_id="muji", value="VariantValue", new_value="VariantValue",
        extra_fields=[], node_key_template="",
    )

    # 不应该抛错
    await upsert_term_with_node_key(
        conn, tenant_id="muji", node_key="Variant:dim_007:00001", standard_name="抹茶",
        aliases=[], term_type="VariantValue", product_line="MUJI",
        extra_properties={"numeric_value": 70},
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v -k "upsert_term_with_node_key"`
Expected: 全部 FAIL

- [ ] **Step 3: 实现**

在 `app/graphrag/terms_store.py` 末尾新增：

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
) -> None:
    """ETL 专用的幂等写入：按 (tenant_id, node_key) 判定冲突，已存在就更新，不存在
    就插入——不是 create_term/update_term 那种"创建 xor 更新"两态分支，是真正的
    upsert，与 Neo4j 侧 merge_relation/sync_term 的 MERGE 语义一致（见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 5 节）。

    node_key 由调用方显式提供（按 node_key_template 算出），不像 create_term 那样
    自动取 standard_name 的值——这是与 create_term/update_term 唯一的本质区别。

    standard_name 的租户内唯一性约束（idx_terms_tenant_standard_name）仍然生效：
    如果这个 standard_name 已经被另一个 node_key 占用，抛 TermNameConflictError。
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
            "product_line, extra_properties) VALUES (?, ?, ?, ?, ?, ?, ?) "
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
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(
            f"{standard_name!r} 已经是租户 {tenant_id!r} 下另一个术语的标准名，无法写入"
        )
    await conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/terms_store.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): add upsert_term_with_node_key for ETL writes keyed by explicit node_key"
```

---

### Task 3: 列映射配置解析

**Files:**
- Create: `app/graphrag/schema_etl_config.py`
- Test: `tests/graphrag/test_schema_etl_config.py`

**Interfaces:**
- Produces：
  - `ColumnNodeKeyPart(column: str)` —— frozen dataclass
  - `AllocatedCodeNodeKeyPart(scope_columns: list[str], raw_value_column: str)` —— frozen dataclass
  - `EntityMapping(term_type: str, source_file: str, product_line: str, standard_name_column: str, node_key_parts: list[ColumnNodeKeyPart | AllocatedCodeNodeKeyPart], field_mappings: dict[str, str])` —— frozen dataclass
  - `RelationMapping(relation_type: str, source_file: str, subject_term_type: str, object_term_type: str)` —— frozen dataclass
  - `SchemaETLConfig(tenant_id: str, entities: list[EntityMapping], relations: list[RelationMapping])` —— frozen dataclass
  - `InvalidSchemaETLConfigError(Exception)`
  - `def load_schema_etl_config(path: Path) -> SchemaETLConfig`

- [ ] **Step 1: 写失败的测试**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from app.graphrag.schema_etl_config import (
    AllocatedCodeNodeKeyPart,
    ColumnNodeKeyPart,
    EntityMapping,
    InvalidSchemaETLConfigError,
    RelationMapping,
    load_schema_etl_config,
)


def test_load_schema_etl_config_parses_entities_and_relations(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji

entities:
  - term_type: Product
    source_file: products.csv
    product_line: "MUJI"
    standard_name_column: product_group_name
    node_key_parts:
      - column: product_group_id
    field_mappings:
      md_no: md_no

  - term_type: VariantValue
    source_file: variant_values.csv
    product_line: "MUJI"
    standard_name_column: label_cn
    node_key_parts:
      - column: dim_code
      - allocated_code:
          scope_columns: [dim_code]
          raw_value_column: raw_value
    field_mappings:
      numeric_value: numeric_value

relations:
  - relation_type: HAS_SKU
    source_file: skus.csv
    subject_term_type: Product
    object_term_type: SKU
""",
        encoding="utf-8",
    )

    config = load_schema_etl_config(config_path)

    assert config.tenant_id == "muji"
    assert len(config.entities) == 2
    product = config.entities[0]
    assert product.term_type == "Product"
    assert product.source_file == "products.csv"
    assert product.product_line == "MUJI"
    assert product.standard_name_column == "product_group_name"
    assert product.node_key_parts == [ColumnNodeKeyPart(column="product_group_id")]
    assert product.field_mappings == {"md_no": "md_no"}

    variant = config.entities[1]
    assert variant.node_key_parts == [
        ColumnNodeKeyPart(column="dim_code"),
        AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
    ]

    assert len(config.relations) == 1
    relation = config.relations[0]
    assert relation == RelationMapping(
        relation_type="HAS_SKU", source_file="skus.csv",
        subject_term_type="Product", object_term_type="SKU",
    )


def test_load_schema_etl_config_rejects_missing_tenant_id(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("entities: []\nrelations: []\n", encoding="utf-8")

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)


def test_load_schema_etl_config_rejects_entity_with_no_node_key_parts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
tenant_id: muji
entities:
  - term_type: Product
    source_file: products.csv
    product_line: "MUJI"
    standard_name_column: name
    node_key_parts: []
    field_mappings: {}
relations: []
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidSchemaETLConfigError):
        load_schema_etl_config(config_path)


def test_load_schema_etl_config_defaults_entities_and_relations_to_empty_list(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tenant_id: muji\n", encoding="utf-8")

    config = load_schema_etl_config(config_path)

    assert config.entities == []
    assert config.relations == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl_config.py -v`
Expected: FAIL——模块不存在

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class InvalidSchemaETLConfigError(Exception):
    """列映射配置格式不合法——缺 tenant_id、entity 没有 node_key_parts 等。"""


@dataclass(frozen=True)
class ColumnNodeKeyPart:
    column: str


@dataclass(frozen=True)
class AllocatedCodeNodeKeyPart:
    scope_columns: list[str]
    raw_value_column: str


@dataclass(frozen=True)
class EntityMapping:
    term_type: str
    source_file: str
    product_line: str
    standard_name_column: str
    node_key_parts: list[ColumnNodeKeyPart | AllocatedCodeNodeKeyPart]
    field_mappings: dict[str, str]


@dataclass(frozen=True)
class RelationMapping:
    relation_type: str
    source_file: str
    subject_term_type: str
    object_term_type: str


@dataclass(frozen=True)
class SchemaETLConfig:
    tenant_id: str
    entities: list[EntityMapping]
    relations: list[RelationMapping]


def _parse_node_key_part(raw: dict) -> ColumnNodeKeyPart | AllocatedCodeNodeKeyPart:
    if "column" in raw:
        return ColumnNodeKeyPart(column=raw["column"])
    if "allocated_code" in raw:
        allocated = raw["allocated_code"]
        return AllocatedCodeNodeKeyPart(
            scope_columns=list(allocated["scope_columns"]),
            raw_value_column=allocated["raw_value_column"],
        )
    raise InvalidSchemaETLConfigError(
        f"node_key_parts 元素必须是 {{'column': ...}} 或 {{'allocated_code': ...}}，收到: {raw!r}"
    )


def _parse_entity_mapping(raw: dict) -> EntityMapping:
    node_key_parts_raw = raw.get("node_key_parts") or []
    if not node_key_parts_raw:
        raise InvalidSchemaETLConfigError(
            f"实体类型 {raw.get('term_type')!r} 的 node_key_parts 不能为空"
        )
    return EntityMapping(
        term_type=raw["term_type"],
        source_file=raw["source_file"],
        product_line=raw["product_line"],
        standard_name_column=raw["standard_name_column"],
        node_key_parts=[_parse_node_key_part(part) for part in node_key_parts_raw],
        field_mappings=dict(raw.get("field_mappings") or {}),
    )


def _parse_relation_mapping(raw: dict) -> RelationMapping:
    return RelationMapping(
        relation_type=raw["relation_type"],
        source_file=raw["source_file"],
        subject_term_type=raw["subject_term_type"],
        object_term_type=raw["object_term_type"],
    )


def load_schema_etl_config(path: Path) -> SchemaETLConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "tenant_id" not in data:
        raise InvalidSchemaETLConfigError(f"配置文件缺少 tenant_id: {path}")
    return SchemaETLConfig(
        tenant_id=data["tenant_id"],
        entities=[_parse_entity_mapping(raw) for raw in data.get("entities") or []],
        relations=[_parse_relation_mapping(raw) for raw in data.get("relations") or []],
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl_config.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/schema_etl_config.py tests/graphrag/test_schema_etl_config.py
git commit -m "feat(graphrag): parse declarative YAML column-mapping config for schema_etl"
```

---

### Task 4: node_key 计算与类型转换

**Files:**
- Create: `app/graphrag/schema_etl_row_processing.py`
- Test: `tests/graphrag/test_schema_etl_row_processing.py`

**Interfaces:**
- Consumes：Task 1 的 `allocate_stable_code`；Task 3 的 `ColumnNodeKeyPart`/`AllocatedCodeNodeKeyPart`；`app/graphrag/ontology_categories.py` 的 `ExtraFieldSpec`
- Produces：
  - `class RowProcessingError(Exception)` —— 单行处理失败时抛出，携带原因
  - `async def compute_node_key(conn, *, tenant_id: str, term_type: str, node_key_parts: list[ColumnNodeKeyPart | AllocatedCodeNodeKeyPart], row: dict[str, str]) -> str`
  - `def convert_field_value(*, extra_field_specs: dict[str, ExtraFieldSpec], field_name: str, raw_value: str) -> object`

- [ ] **Step 1: 写失败的测试**

```python
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import AllocatedCodeNodeKeyPart, ColumnNodeKeyPart
from app.graphrag.schema_etl_row_processing import (
    RowProcessingError,
    compute_node_key,
    convert_field_value,
)

pytestmark = pytest.mark.anyio


async def test_compute_node_key_with_direct_column_only():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    node_key = await compute_node_key(
        conn, tenant_id="muji", term_type="Product",
        node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
        row={"product_group_id": "1001", "product_group_name": "圆角收纳盒"},
    )

    assert node_key == "Product:1001"


async def test_compute_node_key_with_allocated_code_part():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    node_key = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue",
        node_key_parts=[
            ColumnNodeKeyPart(column="dim_code"),
            AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
        ],
        row={"dim_code": "dim_007", "raw_value": "抹茶"},
    )

    assert node_key == "Variant:dim_007:00001"


async def test_compute_node_key_reuses_allocated_code_across_calls():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)
    parts = [
        ColumnNodeKeyPart(column="dim_code"),
        AllocatedCodeNodeKeyPart(scope_columns=["dim_code"], raw_value_column="raw_value"),
    ]

    first = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue", node_key_parts=parts,
        row={"dim_code": "dim_007", "raw_value": "抹茶"},
    )
    second = await compute_node_key(
        conn, tenant_id="muji", term_type="VariantValue", node_key_parts=parts,
        row={"dim_code": "dim_007", "raw_value": "抹茶"},
    )

    assert first == second == "Variant:dim_007:00001"


async def test_compute_node_key_raises_when_column_missing_from_row():
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    with pytest.raises(RowProcessingError):
        await compute_node_key(
            conn, tenant_id="muji", term_type="Product",
            node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
            row={"other_column": "x"},
        )


async def test_compute_node_key_raises_when_column_present_but_empty():
    """CSV 里一个空单元格解析出来是存在的空字符串，不是"键不存在"——
    必须单独检查空值，不能只判断列名在不在 row 里。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)

    with pytest.raises(RowProcessingError):
        await compute_node_key(
            conn, tenant_id="muji", term_type="Product",
            node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
            row={"product_group_id": ""},
        )


def test_convert_field_value_number():
    specs = {"numeric_value": ExtraFieldSpec(name="numeric_value", value_type="number")}
    assert convert_field_value(extra_field_specs=specs, field_name="numeric_value", raw_value="750") == 750.0


def test_convert_field_value_integer():
    specs = {"sku_count": ExtraFieldSpec(name="sku_count", value_type="integer")}
    assert convert_field_value(extra_field_specs=specs, field_name="sku_count", raw_value="12") == 12


def test_convert_field_value_string():
    specs = {"md_no": ExtraFieldSpec(name="md_no", value_type="string")}
    assert convert_field_value(extra_field_specs=specs, field_name="md_no", raw_value="A123") == "A123"


def test_convert_field_value_number_array_splits_on_semicolon():
    specs = {"dims": ExtraFieldSpec(name="dims", value_type="number[]")}
    result = convert_field_value(extra_field_specs=specs, field_name="dims", raw_value="20.5;10.0")
    assert result == [20.5, 10.0]


def test_convert_field_value_raises_when_field_not_declared():
    specs: dict = {}
    with pytest.raises(RowProcessingError):
        convert_field_value(extra_field_specs=specs, field_name="unknown_field", raw_value="x")


def test_convert_field_value_raises_on_non_numeric_string_for_number_type():
    specs = {"numeric_value": ExtraFieldSpec(name="numeric_value", value_type="number")}
    with pytest.raises(RowProcessingError):
        convert_field_value(extra_field_specs=specs, field_name="numeric_value", raw_value="不是数字")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl_row_processing.py -v`
Expected: FAIL——模块不存在

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

from app.graphrag.etl_stable_code_registry import allocate_stable_code
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import AllocatedCodeNodeKeyPart, ColumnNodeKeyPart

import aiosqlite


class RowProcessingError(Exception):
    """处理某一行源数据时失败——列缺失、值转换失败、字段未声明等。写入引擎
    捕获这个异常，按 docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md
    第 6.4 节的策略跳过该行、记录日志，不中断整批。"""


async def compute_node_key(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    node_key_parts: list[ColumnNodeKeyPart | AllocatedCodeNodeKeyPart],
    row: dict[str, str],
) -> str:
    """按 node_key_parts 依次解析出各部分的值，用英文冒号拼接，前面加
    "{term_type}:" 前缀——见计划的 Global Constraints。"""
    parts: list[str] = []
    for part in node_key_parts:
        if isinstance(part, ColumnNodeKeyPart):
            if not row.get(part.column):
                raise RowProcessingError(f"node_key 需要的列 {part.column!r} 在这一行不存在或为空")
            parts.append(row[part.column])
        else:
            for scope_column in part.scope_columns:
                if not row.get(scope_column):
                    raise RowProcessingError(f"node_key 需要的作用域列 {scope_column!r} 在这一行不存在或为空")
            if not row.get(part.raw_value_column):
                raise RowProcessingError(f"node_key 需要的原始值列 {part.raw_value_column!r} 在这一行不存在或为空")
            scope = ":".join([term_type, *[row[c] for c in part.scope_columns]])
            raw_value = row[part.raw_value_column]
            allocated_code = await allocate_stable_code(
                conn, tenant_id=tenant_id, scope=scope, raw_value=raw_value
            )
            parts.append(allocated_code)
    return f"{term_type}:" + ":".join(parts)


def convert_field_value(
    *, extra_field_specs: dict[str, ExtraFieldSpec], field_name: str, raw_value: str
) -> object:
    """按已确认 schema 里该字段声明的 value_type，把 CSV 读出来的原始字符串
    转换成对应的 Python 类型——见 spec 第 4 节的转换规则表。extra_field_specs
    由调用方在处理某个 term_type 的整个源文件之前查询一次、传进来，不在这个
    函数里重复查库（避免大文件逐行查询数据库）。
    """
    if field_name not in extra_field_specs:
        raise RowProcessingError(f"字段 {field_name!r} 没有在 term_type 的 schema 里声明")
    value_type = extra_field_specs[field_name].value_type
    try:
        if value_type == "string":
            return raw_value
        if value_type == "number":
            return float(raw_value)
        if value_type == "integer":
            return int(raw_value)
        if value_type == "number[]":
            return [float(item) for item in raw_value.split(";") if item.strip()]
    except ValueError:
        raise RowProcessingError(
            f"字段 {field_name!r} 的值 {raw_value!r} 无法转换成声明的类型 {value_type!r}"
        )
    raise RowProcessingError(f"字段 {field_name!r} 声明了未知的 value_type: {value_type!r}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl_row_processing.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/schema_etl_row_processing.py tests/graphrag/test_schema_etl_row_processing.py
git commit -m "feat(graphrag): compute ETL node_key and convert typed field values from CSV strings"
```

---

### Task 5: 写入引擎主体 + CLI

**Files:**
- Create: `app/graphrag/schema_etl.py`
- Modify: `app/graphrag/provenance.py`
- Test: `tests/graphrag/test_schema_etl.py`

**Interfaces:**
- Consumes：Task 1 的 `ensure_stable_code_registry_schema`；Task 2 的 `upsert_term_with_node_key`；Task 3 的 `load_schema_etl_config`/`EntityMapping`/`RelationMapping`；Task 4 的 `compute_node_key`/`convert_field_value`/`RowProcessingError`；`app/graphrag/ontology_categories.py::list_term_types`；`app/graphrag/ontology_lifecycle.py::is_ontology_confirmed`；`app/graphrag/neo4j_client.py::Neo4jGraphClient.sync_term`/`merge_relation`；`app/graphrag/ontology.py::Term`
- Produces：
  - `class SchemaETLNotConfirmedError(Exception)`
  - `@dataclass class ETLRunReport` —— 汇总报告
  - `async def run_schema_etl(*, conn, graph_client, config: SchemaETLConfig, data_dir: Path) -> ETLRunReport`

- [ ] **Step 1: 给 provenance.py 新增 ETL 常量**

`app/graphrag/provenance.py` 完整替换：

```python
from __future__ import annotations

# Neo4jGraphClient.merge_relation() 的 provenance 参数只允许这三个取值，
# 集中定义在这里供所有写入路径共用（app/graphrag/normalization.py 的自动
# 写入路径、app/graphrag/review_queue.py 的人工批准路径、app/graphrag/
# schema_etl.py 的结构化 ETL 写入路径），避免字符串字面量在多处各写一份、
# 容易打错或不同步。
#
# ETL 值是 2026-08-16 补的：ETL 数据既不是"摄取时术语表精确对齐后自动写入"
# （AUTO_MERGED 的原意特指抽取管道），也不是人工审核批准（HUMAN_APPROVED）——
# 是结构化确定性数据源直接写入，语义上是第三类，不套用前两个值。
AUTO_MERGED = "auto_merged"
HUMAN_APPROVED = "human_approved"
ETL = "etl"
```

- [ ] **Step 2: 写失败的测试**

```python
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec, create_product_line, create_term_type
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.schema_etl import SchemaETLNotConfirmedError, run_schema_etl
from app.graphrag.schema_etl_config import ColumnNodeKeyPart, EntityMapping, RelationMapping, SchemaETLConfig
from app.graphrag.terms_store import ensure_terms_schema, get_term

pytestmark = pytest.mark.anyio


class FakeGraphClient:
    def __init__(self) -> None:
        self.synced: list[str] = []
        self.merged: list[tuple[str, str, str]] = []

    async def sync_term(self, term) -> None:
        self.synced.append(term.node_key)

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type,
        source, tenant_id, provenance, recorded_at,
    ) -> None:
        self.merged.append((subject_standard_name, object_standard_name, relation_type))


async def _confirmed_conn(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    await create_term_type(
        conn, tenant_id="muji", value="Product",
        extra_fields=[ExtraFieldSpec(name="md_no", value_type="string")],
    )
    await create_term_type(conn, tenant_id="muji", value="SKU")
    await create_product_line(conn, value="MUJI")
    await checkout_draft(conn, "muji")
    await create_relation_type(
        conn, "muji", relation_type="HAS_SKU", example_phrase="Product HAS_SKU SKU",
    )
    await confirm_ontology(conn, "muji")
    return conn


async def test_run_schema_etl_raises_when_schema_not_confirmed():
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    config = SchemaETLConfig(tenant_id="muji", entities=[], relations=[])

    with pytest.raises(SchemaETLNotConfirmedError):
        await run_schema_etl(conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=Path("."))


async def test_run_schema_etl_writes_entities_and_relations(tmp_path):
    conn = await _confirmed_conn(tmp_path)
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    (tmp_path / "skus.csv").write_text(
        "jan,product_group_id\n4901234567890,1001\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv", product_line="MUJI",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
            EntityMapping(
                term_type="SKU", source_file="skus.csv", product_line="MUJI",
                standard_name_column="jan",
                node_key_parts=[ColumnNodeKeyPart(column="jan")],
                field_mappings={},
            ),
        ],
        relations=[
            RelationMapping(
                relation_type="HAS_SKU", source_file="skus.csv",
                subject_term_type="Product", object_term_type="SKU",
            ),
        ],
    )
    graph_client = FakeGraphClient()

    report = await run_schema_etl(conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path)

    assert report.entities_written == 2
    assert report.entities_skipped == 0
    assert report.relations_written == 1
    product = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert product.node_key == "Product:1001"
    assert product.extra_properties == {"md_no": "A123"}
    assert "Product:1001" in graph_client.synced
    assert "SKU:4901234567890" in graph_client.synced
    assert ("Product:1001", "SKU:4901234567890", "HAS_SKU") in graph_client.merged


async def test_run_schema_etl_skips_bad_row_and_reports_it(tmp_path):
    conn = await _confirmed_conn(tmp_path)
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n"
        "1001,圆角收纳盒,A123\n"
        ",没有ID的商品,B456\n",  # 第二行缺 product_group_id
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv", product_line="MUJI",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.entities_skipped == 1
    assert len(report.skipped_rows) == 1
    assert "products.csv" in report.skipped_rows[0].source_file


async def test_run_schema_etl_rerun_is_idempotent(tmp_path):
    conn = await _confirmed_conn(tmp_path)
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv", product_line="MUJI",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    await run_schema_etl(conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path)
    await run_schema_etl(conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path)

    from app.graphrag.terms_store import list_terms
    all_terms = await list_terms(conn, tenant_id="muji")
    assert len(all_terms) == 1
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl.py -v`
Expected: FAIL——模块不存在

- [ ] **Step 4: 实现**

```python
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag import provenance
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.review_factory import build_review_conn_from_settings
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping, SchemaETLConfig, load_schema_etl_config
from app.graphrag.schema_etl_row_processing import RowProcessingError, compute_node_key, convert_field_value
from app.graphrag.terms_store import TermNameConflictError, get_term, upsert_term_with_node_key


class SchemaETLNotConfirmedError(Exception):
    """该租户的本体 schema 还没有 confirm，拒绝运行 ETL——见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 6.2 节。"""


@dataclass
class SkippedRow:
    source_file: str
    row_number: int
    reason: str


@dataclass
class ETLRunReport:
    entities_written: int = 0
    entities_skipped: int = 0
    relations_written: int = 0
    relations_skipped: int = 0
    skipped_rows: list[SkippedRow] = field(default_factory=list)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


async def _write_entity_mapping(
    *,
    conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    tenant_id: str,
    mapping: EntityMapping,
    data_dir: Path,
    report: ETLRunReport,
) -> None:
    term_types = await list_term_types(conn, tenant_id)
    types_by_value = {t.value: t for t in term_types}
    if mapping.term_type not in types_by_value:
        raise RowProcessingError(f"term_type {mapping.term_type!r} 不在已确认 schema 里")
    extra_field_specs = {f.name: f for f in types_by_value[mapping.term_type].extra_fields}

    rows = _read_csv_rows(data_dir / mapping.source_file)
    for row_number, row in enumerate(rows, start=2):  # 第 1 行是表头
        try:
            node_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.term_type,
                node_key_parts=mapping.node_key_parts, row=row,
            )
            if not row.get(mapping.standard_name_column):
                raise RowProcessingError(f"standard_name 需要的列 {mapping.standard_name_column!r} 不存在或为空")
            standard_name = row[mapping.standard_name_column]
            extra_properties = {
                field_name: convert_field_value(
                    extra_field_specs=extra_field_specs, field_name=field_name,
                    raw_value=row[source_column],
                )
                for field_name, source_column in mapping.field_mappings.items()
                if source_column in row and row[source_column]
            }
            await upsert_term_with_node_key(
                conn, tenant_id=tenant_id, node_key=node_key, standard_name=standard_name,
                aliases=[], term_type=mapping.term_type, product_line=mapping.product_line,
                extra_properties=extra_properties,
            )
            term = Term(
                tenant_id=tenant_id, node_key=node_key, standard_name=standard_name,
                aliases=[], term_type=mapping.term_type, product_line=mapping.product_line,
                extra_properties=extra_properties,
            )
            await graph_client.sync_term(term)
            report.entities_written += 1
        except (RowProcessingError, TermNameConflictError) as exc:
            report.entities_skipped += 1
            report.skipped_rows.append(
                SkippedRow(source_file=mapping.source_file, row_number=row_number, reason=str(exc))
            )


async def _write_relation_mapping(
    *,
    conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    tenant_id: str,
    mapping: RelationMapping,
    entity_mappings_by_term_type: dict[str, EntityMapping],
    data_dir: Path,
    report: ETLRunReport,
) -> None:
    subject_entity = entity_mappings_by_term_type.get(mapping.subject_term_type)
    object_entity = entity_mappings_by_term_type.get(mapping.object_term_type)
    if subject_entity is None or object_entity is None:
        raise RowProcessingError(
            f"关系 {mapping.relation_type!r} 引用的实体类型未在 entities 段声明"
        )
    rows = _read_csv_rows(data_dir / mapping.source_file)
    for row_number, row in enumerate(rows, start=2):
        try:
            subject_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.subject_term_type,
                node_key_parts=subject_entity.node_key_parts, row=row,
            )
            object_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.object_term_type,
                node_key_parts=object_entity.node_key_parts, row=row,
            )
            await graph_client.merge_relation(
                subject_standard_name=subject_key, object_standard_name=object_key,
                relation_type=mapping.relation_type, source=mapping.source_file,
                tenant_id=tenant_id, provenance=provenance.ETL, recorded_at=datetime.now(),
            )
            report.relations_written += 1
        except RowProcessingError as exc:
            report.relations_skipped += 1
            report.skipped_rows.append(
                SkippedRow(source_file=mapping.source_file, row_number=row_number, reason=str(exc))
            )


async def run_schema_etl(
    *, conn: aiosqlite.Connection, graph_client: Neo4jGraphClient, config: SchemaETLConfig, data_dir: Path
) -> ETLRunReport:
    """按已确认 schema + 列映射配置，把 CSV 源数据确定性写入 Term/Neo4j 双存储。
    见 docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 6 节。
    """
    if not await is_ontology_confirmed(conn, config.tenant_id):
        raise SchemaETLNotConfirmedError(
            f"租户 {config.tenant_id!r} 的本体 schema 还没有确认，拒绝运行 ETL"
        )
    await ensure_stable_code_registry_schema(conn)

    report = ETLRunReport()
    for entity_mapping in config.entities:
        await _write_entity_mapping(
            conn=conn, graph_client=graph_client, tenant_id=config.tenant_id,
            mapping=entity_mapping, data_dir=data_dir, report=report,
        )

    entity_mappings_by_term_type = {m.term_type: m for m in config.entities}
    for relation_mapping in config.relations:
        await _write_relation_mapping(
            conn=conn, graph_client=graph_client, tenant_id=config.tenant_id,
            mapping=relation_mapping, entity_mappings_by_term_type=entity_mappings_by_term_type,
            data_dir=data_dir, report=report,
        )

    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按列映射配置把结构化 CSV 数据写入知识图谱")
    parser.add_argument("--config", required=True, type=Path, help="列映射 YAML 配置文件路径")
    parser.add_argument("--data-dir", required=True, type=Path, help="配置里 source_file 相对路径的基准目录")
    return parser.parse_args()


async def _main(*, config_path: Path, data_dir: Path) -> None:
    settings = Settings()
    config = load_schema_etl_config(config_path)
    conn = await build_review_conn_from_settings(settings)
    graph_client = build_graph_client_from_settings(settings)
    try:
        report = await run_schema_etl(conn=conn, graph_client=graph_client, config=config, data_dir=data_dir)
    finally:
        await conn.close()
    print(
        f"实体写入 {report.entities_written} 条，跳过 {report.entities_skipped} 条；"
        f"关系写入 {report.relations_written} 条，跳过 {report.relations_skipped} 条"
    )
    for skipped in report.skipped_rows:
        print(f"  跳过 {skipped.source_file} 第 {skipped.row_number} 行：{skipped.reason}")


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(_main(config_path=args.config, data_dir=args.data_dir))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 全量回归测试**

Run: `.venv/Scripts/python.exe -u -m pytest -q`
Expected: 除了预先已知的、与本计划无关的 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured` 之外，全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add app/graphrag/schema_etl.py app/graphrag/provenance.py tests/graphrag/test_schema_etl.py
git commit -m "feat(graphrag): add schema_etl.py write engine with CLI entry point"
```

---

## Self-Review（写计划人自查，非 subagent 执行）

**Spec 覆盖检查**（对照 `docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md`）：
- §1 源数据形态（文件、只读 CSV）→ Task 5 的 `_read_csv_rows` ✅
- §2 列映射配置格式 → Task 3 ✅
- §3 稳定码注册机制 → Task 1 ✅
- §4 类型转换 → Task 4 的 `convert_field_value` ✅
- §5 terms_store.py 新增写入接口 → Task 2 ✅
- §6.1 CLI 入口惯例 → Task 5 的 `_parse_args`/`if __name__ == "__main__":` ✅
- §6.2 schema-confirmed 前置门禁 → Task 5 的 `run_schema_etl` 开头检查 ✅
- §6.3 处理阶段顺序（entities 先于 relations）→ Task 5 的 `run_schema_etl` 主循环顺序 ✅
- §6.4 单行容错策略 → Task 5 的 `_write_entity_mapping`/`_write_relation_mapping` 里的 `try/except RowProcessingError` + `ETLRunReport.skipped_rows` ✅
- §6.5 重跑语义（只增不删）→ Task 2/5 全程只用 upsert/MERGE，没有任何 DELETE 语句 ✅
- §7 范围之外的条目均未在任何任务里实现，符合预期 ✅

**占位符扫描**：全文所有代码块均为可直接运行的完整实现/测试，无 TBD/TODO 类占位表述。

**类型一致性检查**：`ColumnNodeKeyPart`/`AllocatedCodeNodeKeyPart`（Task 3 定义）在 Task 4/5 的函数签名和实现里保持同名同结构，未出现漂移。`RowProcessingError`（Task 4 定义）在 Task 5 里统一捕获处理，没有出现 Task 5 自己发明一个不同的异常类型来表达同一件事。`ETLRunReport`/`SkippedRow`（Task 5 定义）字段名贯穿测试和实现保持一致。`node_key` 的拼接规则（`f"{term_type}:" + ":".join(parts)`）在 Task 4 的 `compute_node_key` 与 Global Constraints 的描述完全一致。
