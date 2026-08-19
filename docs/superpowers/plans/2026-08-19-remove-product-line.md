# 移除 product_line（产品线）概念 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 彻底从数据模型、后台管理 UI、ETL 配置、结构化检索、LLM 上下文展示里删除 `product_line`（产品线）这个概念——不是弱化成可选字段，是整条功能线连根拔起。

**Architecture:** 这是一次"删除已有能力"而非"新增能力"的改造，跟本仓库大多数 plan 相反：`Term`/`create_term` 等核心签名一旦在某个任务里去掉 `product_line` 参数，所有下游调用方在那之前都会保持"能跑但类型不对/字段多余"的中间状态，直到对应任务把它们也改掉——这是预期中的、暂时的构建期不一致，不是回归。全部 8 个改造任务共同的依赖根是 Task 2（`Term` dataclass + `terms_store.py`），其余任务都要等它先落地。

**Tech Stack:** FastAPI + aiosqlite（后端）、React + TypeScript（前端）、Neo4j（图谱存储）。

**Spec:** `docs/superpowers/specs/2026-08-19-remove-product-line-design.md`

## Global Constraints

- **不可逆数据删除，不做备份**：`terms` 表的 `product_line` 列用原生 `ALTER TABLE terms DROP COLUMN product_line` 直接删除（SQLite 3.35+ 原生支持，本项目实测 3.49.1）；删除前不导出/记日志（spec 决策 1、2）。
- **Neo4j 历史属性不清理**：只改代码不再读/写 `:Term` 节点的 `product_line` 属性，不批量遍历图谱做 `REMOVE`（spec 决策 3）。
- **ETL 配置格式硬切，不兼容旧 YAML**：`EntityMapping.product_line` 直接从 schema 删除，不做过渡期兼容（spec 决策 4）。
- **agent 结构化过滤查询里 `product_line` 直接摘除**：它从来不是 `AttributeConstraint.field` 的可用过滤维度，只是查询结果行的展示字段，删除它只是结果行少一个 key（spec 决策 5）。
- 每个任务完成后只运行**该任务自己触及的测试文件**，不强求当时全量套件是绿的（下游任务还没改，会有大量因 `product_line` 参数不匹配导致的失败，这是预期状态）——只有最后一个任务（Task 9）负责全量验证。

---

## Task 1: 删除产品线管理功能（`ontology_categories.py` CRUD + `admin_ontology_routes.py` 路由）

**Files:**
- Modify: `app/graphrag/ontology_categories.py`
- Modify: `app/api/admin_ontology_routes.py`
- Test: `tests/graphrag/test_ontology_categories.py`
- Test: `tests/api/test_admin_ontology_routes.py`

**Interfaces:**
- Consumes：无新增外部依赖。
- Produces：`list_product_lines`/`create_product_line`/`update_product_line`/`delete_product_line` 四个函数、`ontology_product_lines` 表、`GET/POST/PUT/DELETE /api/admin/ontology/product-lines[/...]` 四个路由**全部不再存在**——这是本任务对下游任务（尤其是 Task 2 的 `_bridge_seed_categories_from_existing_terms`、Task 7/8 前端）的破坏性变更，下游任务预期会因此暂时报 `ImportError`/编译错误，属于正常中间状态。

- [ ] **Step 1: `_SCHEMA_SQL` 去掉 `ontology_product_lines` 建表语句，加一条 DROP TABLE 迁移**

`app/graphrag/ontology_categories.py` 第 9-21 行：

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_term_types (
    tenant_id         TEXT NOT NULL,
    value             TEXT NOT NULL,
    extra_fields      TEXT NOT NULL DEFAULT '[]',
    node_key_template TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL,
    PRIMARY KEY (tenant_id, value, status)
);
"""
```

`ensure_categories_schema`（第 203-208 行）在最前面加一句无条件的 `DROP TABLE IF EXISTS`（幂等，表不存在时是空操作，不需要额外的存在性探测）：

```python
async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("DROP TABLE IF EXISTS ontology_product_lines")
    await _migrate_term_types_table_if_needed(conn)
    await _migrate_term_types_add_status_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    await _migrate_extra_fields_value_shape_if_needed(conn)
```

- [ ] **Step 2: 删除四个产品线 CRUD 函数**

`app/graphrag/ontology_categories.py` 里删除以下四段（它们跟 `term_type` 的同名函数交错分布，不在同一个连续区块，逐个删）：
- `list_product_lines`（第 231-234 行）
- `create_product_line`（第 257-264 行）
- `update_product_line`（第 310-328 行）
- `delete_product_line`（第 357-364 行）

`CategoryInUseError` 的 docstring（第 32-35 行）删掉 `product_line` 的提及，改成：

```python
class CategoryInUseError(Exception):
    """删除的分类枚举值仍被 terms 表引用，terms.term_type 是硬约束外键，
    删除在用的值会让已有术语行结构失效，必须阻止（不同于关系类型删除——那只是写入
    白名单，不是任何表的外键约束对象，见 ontology_relations.py）。"""
```

- [ ] **Step 3: 删除 `admin_ontology_routes.py` 里产品线相关的一切**

`app/api/admin_ontology_routes.py`：
- import 块（第 11-25 行）里删掉 `create_product_line`/`delete_product_line`/`list_product_lines`/`update_product_line` 四个 name（保留其余 `term_type` 相关的 import）。
- `ProductLineWriteRequest` 模型（第 70-72 行）整块删除。
- `list_product_line_categories`/`create_product_line_category`/`update_product_line_category`/`delete_product_line_category` 四个路由（第 229-271 行区域）整块删除。

- [ ] **Step 4: 更新/删除测试**

`tests/graphrag/test_ontology_categories.py`：删除所有 `create_product_line`/`update_product_line`/`delete_product_line`/`list_product_lines` 的直接单元测试（grep 这个文件里 `product_line` 出现的每一处测试函数，逐个删除整个测试函数）。如果文件顶部 import 了这四个函数，import 也删掉。

`tests/api/test_admin_ontology_routes.py`：删除所有针对 `/product-lines` 路由的测试用例（grep `product-line`/`product_line` 定位）。

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/graphrag/test_ontology_categories.py tests/api/test_admin_ontology_routes.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/ontology_categories.py app/api/admin_ontology_routes.py tests/graphrag/test_ontology_categories.py tests/api/test_admin_ontology_routes.py
git commit -m "feat: remove product-line management feature (CRUD + admin routes)"
```

---

## Task 2: 从 `Term` dataclass 和 `terms_store.py` 核心删除 product_line（本 plan 的依赖根）

**Files:**
- Modify: `app/graphrag/ontology.py`
- Modify: `app/graphrag/terminology_seed.yaml`
- Modify: `app/graphrag/terms_store.py`
- Test: `tests/graphrag/test_terms_store.py`
- Test: `tests/graphrag/test_ontology.py`

**Interfaces:**
- Consumes：Task 1 完成后 `ontology_categories.py` 不再导出 `list_product_lines`（本任务是这个符号最后一个调用方，删除后这个 import 名字彻底从代码库消失）。
- Produces：`Term` dataclass 不再有 `product_line` 字段；`create_term`/`update_term`/`upsert_term_with_node_key`/`_validate_categories` 不再接受 `product_line` 参数；`terms` 表不再有 `product_line` 列——**这是本 plan 里唯一的不可逆数据删除动作**。下游 Task 3-6、8 都依赖这次改动，在它们各自完成前会因为仍在传 `product_line` 关键字参数而报 `TypeError: unexpected keyword argument`，属预期中间状态。

- [ ] **Step 1: `Term` dataclass 删除 `product_line` 字段**

`app/graphrag/ontology.py` 第 9-18 行：

```python
@dataclass(frozen=True)
class Term:
    tenant_id: str
    node_key: str
    standard_name: str
    aliases: list[str]
    term_type: str
    extra_properties: dict[str, str | int | float | list[float]] = field(default_factory=dict)
    source: str = "unknown"
```

`load_terminology`（第 21-46 行）的 `Term(...)` 构造（第 37-44 行）删掉 `product_line=str(item.get("product_line", ""))` 那一行；函数 docstring 第 22 行"加载人工维护的术语表（标准名称+别名+类型+产品线）"改成"加载人工维护的术语表（标准名称+别名+类型）"。

- [ ] **Step 2: 种子 YAML 删除两行 `product_line`**

`app/graphrag/terminology_seed.yaml`：删除第 9 行和第 13 行的 `product_line: 示例产品线`。

- [ ] **Step 3: `terms_store.py` 的 import 收窄**

`app/graphrag/terms_store.py` 第 11-15 行：

```python
from app.graphrag.ontology_categories import (
    ensure_categories_schema,
    list_term_types,
)
```

- [ ] **Step 4: `_SCHEMA_SQL` 删掉 `product_line` 列**

第 19-33 行：

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    tenant_id         TEXT NOT NULL,
    node_key          TEXT NOT NULL,
    standard_name     TEXT NOT NULL,
    aliases           TEXT NOT NULL,
    term_type         TEXT NOT NULL,
    extra_properties  TEXT NOT NULL DEFAULT '{}',
    source            TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (tenant_id, node_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_terms_tenant_standard_name
    ON terms(tenant_id, standard_name);
"""
```

- [ ] **Step 5: 老版本（pre-2026-08-15）迁移路径同步去掉 product_line**

`_migrate_terms_table_to_tenant_scoped_if_needed`（第 58-104 行）——这个函数只在 `terms` 表还没有 `tenant_id` 列（即极老的库）时触发，会重建整张表，天然是"顺手把 product_line 也去掉"的地方。第 79-90 行 `terms_new` 的建表语句删掉 `product_line TEXT NOT NULL,` 那一行；第 92-98 行的 INSERT 语句：

```python
    await conn.execute(
        "INSERT INTO terms_new "
        "(tenant_id, node_key, standard_name, aliases, term_type, extra_properties, source) "
        "SELECT 'default', standard_name, standard_name, aliases, term_type, "
        "extra_properties, source FROM terms"
    )
```

（注意：这条 SELECT 是从老 `terms` 表读数据——老表可能仍然有 `product_line` 列，SELECT 语句里不选它就是"读取时直接丢弃"，这正是这次要的效果，不需要额外处理。）

- [ ] **Step 6: 新增一个私有迁移函数，处理"已经是新结构、但还带着 product_line 列"的库**

Step 5 的迁移只覆盖极老的库（没有 `tenant_id` 列的）；本项目实际的开发库/任何跑过 `admin-ux-fixes`/`term-type-draft-lifecycle` 之后的库早就有 `tenant_id`，会跳过 Step 5 那个分支，需要单独处理。在 `_migrate_terms_table_to_tenant_scoped_if_needed` 函数之后新增：

```python
async def _migrate_terms_drop_product_line_column_if_needed(
    conn: aiosqlite.Connection,
) -> None:
    """把仍带着 product_line 列的 terms 表（tenant_id 已存在，只是还没删这一列
    的库——本项目实际开发库/生产库的常见情况）原地去掉这一列。SQLite 3.35+
    原生支持 ALTER TABLE ... DROP COLUMN（本项目实测 SQLite 3.49.1），不需要
    像 _migrate_terms_table_to_tenant_scoped_if_needed 那样建新表搬数据——
    product_line 只是普通 TEXT NOT NULL 列，不是主键的一部分、没有 CHECK/
    UNIQUE 约束、不被任何生成列引用，满足原生语法的适用条件。幂等：列已经
    不存在时直接跳过。不做删除前的数据备份，见
    docs/superpowers/specs/2026-08-19-remove-product-line-design.md 决策 2
    （这批数据本身没有实际区分意义，备份没有价值）。
    """
    cursor = await conn.execute("PRAGMA table_info(terms)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "product_line" not in existing_columns:
        return
    await conn.execute("ALTER TABLE terms DROP COLUMN product_line")
    await conn.commit()
```

- [ ] **Step 7: `ensure_terms_schema` 接入新迁移函数，清理 docstring 和种子导入逻辑**

第 107-173 行区域：

在 `if table_already_existed:` 分支里，紧跟着现有的 `_migrate_terms_table_to_tenant_scoped_if_needed(conn)` 调用之后加一行：

```python
    if table_already_existed:
        await add_column_if_missing(
            conn, table="terms", column="extra_properties", ddl="TEXT NOT NULL DEFAULT '{}'"
        )
        await add_column_if_missing(
            conn, table="terms", column="source", ddl="TEXT NOT NULL DEFAULT 'unknown'"
        )
        await _migrate_terms_table_to_tenant_scoped_if_needed(conn)
        await _migrate_terms_drop_product_line_column_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
```

docstring（第 110-124 行）第 117-118 行"向后兼容桥接：分类枚举表为空、但 terms 表已经有历史数据（老版本上线时term_type/product_line 还是自由文本，没有枚举表）"改成"……老版本上线时term_type 还是自由文本，没有枚举表）"。

种子导入逻辑（第 140-156 行）的 INSERT 语句删掉 `product_line` 列和对应的 `term.product_line` 值：

```python
            for term in load_terminology(seed_yaml_path):
                await conn.execute(
                    "INSERT OR IGNORE INTO terms "
                    "(tenant_id, node_key, standard_name, aliases, term_type, "
                    "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        term.tenant_id,
                        term.node_key,
                        term.standard_name,
                        json.dumps(term.aliases, ensure_ascii=False),
                        term.term_type,
                        json.dumps(term.extra_properties, ensure_ascii=False),
                    ),
                )
```

- [ ] **Step 8: `_bridge_seed_categories_from_existing_terms` 去掉产品线桥接**

第 176-206 行：

```python
async def _bridge_seed_categories_from_existing_terms(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> None:
    """桥接函数：分类枚举表为空、但 terms 表已经有历史数据时，把该租户历史数据里
    出现过的去重实体类型值导入枚举表。按租户隔离，每次调用只处理一个租户。
    """
    known_types = await list_term_types(conn, tenant_id, status="confirmed")
    if known_types:
        return
    cursor = await conn.execute(
        "SELECT DISTINCT term_type FROM terms WHERE tenant_id = ?", (tenant_id,)
    )
    distinct_types = [row[0] for row in await cursor.fetchall()]
    if not distinct_types:
        return
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types (tenant_id, value, extra_fields, status) "
            "VALUES (?, ?, '[]', 'confirmed')",
            (tenant_id, value),
        )
    await conn.commit()
```

- [ ] **Step 9: `_row_to_term`/`list_terms`/`get_term` 去掉 product_line**

`_row_to_term`（第 223-233 行）删掉 `product_line=row["product_line"],`。

`list_terms`（第 236-270 行）两处 SELECT 语句都删掉 `product_line, `：

```python
    if source is None:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source FROM terms WHERE tenant_id = ? "
            "ORDER BY standard_name LIMIT ? OFFSET ?",
            (tenant_id, limit if limit is not None else -1, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source FROM terms WHERE tenant_id = ? AND source = ? "
            "ORDER BY standard_name LIMIT ? OFFSET ?",
            (tenant_id, source, limit if limit is not None else -1, offset),
        )
```

`get_term`（第 287-297 行）同样删掉 SELECT 里的 `product_line, `。

- [ ] **Step 10: `_validate_categories` 去掉 product_line 参数和校验**

第 327-365 行：

```python
async def _validate_categories(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    extra_properties: dict[str, object],
    existing_extra_property_keys: frozenset[str] = frozenset(),
) -> None:
    """term_type 校验按租户过滤——每个租户只能使用该租户下注册的分类。

    字段名校验（是否在白名单里）和字段值类型校验（是否匹配声明的
    value_type）是两道独立的检查：existing_extra_property_keys 里的
    "已废弃字段"只豁免字段名检查，不再做类型检查（因为它已经不在
    declared_by_name 里，无法判断"应该是什么类型"）——这是延续本体
    基座计划"移除字段声明不触碰已有数据"的原则，见 Global Constraints。
    """
    types = await list_term_types(conn, tenant_id, status="confirmed")
    types_by_value = {t.value: t for t in types}
    if term_type not in types_by_value:
        raise UnknownCategoryError(f"未知分类: {term_type!r}")
    declared_by_name = {f.name: f for f in types_by_value[term_type].extra_fields}
    declared_fields = set(declared_by_name)
    unknown = set(extra_properties) - declared_fields - existing_extra_property_keys
    if unknown:
        raise UnknownCategoryError(
            f"分类 {term_type!r} 没有声明这些属性字段: {sorted(unknown)}"
        )
    for key, value in extra_properties.items():
        if key not in declared_fields:
            continue
        spec = declared_by_name[key]
        if not _extra_property_value_matches_type(value, spec.value_type):
            raise InvalidExtraPropertyTypeError(
                f"字段 {key!r} 的值 {value!r} 不符合声明的类型 {spec.value_type!r}"
            )
```

`UnknownCategoryError` 类的 docstring（第 47-51 行）删掉"/product_line"：

```python
class UnknownCategoryError(Exception):
    """提交的 term_type 不在全局分类枚举表里，或 extra_properties
    里出现了该 term_type 没有声明过的字段名——本体 schema 基座计划把这两项从
    "自由文本、无校验" 收紧成硬约束，理由见
    docs/superpowers/specs/2026-08-14-ontology-schema-design.md 第 3 节。"""
```

- [ ] **Step 11: `create_term`/`update_term`/`upsert_term_with_node_key` 三个写入函数去掉 product_line**

三个函数结构相同，逐一处理（都在第 368-561 行区域）：

`create_term`：函数签名删掉 `product_line: str,` 参数；对 `_validate_categories(...)` 的调用删掉 `product_line=product_line,`；INSERT 语句的列列表和 VALUES 占位符各少一个，参数元组删掉 `product_line,`：

```python
async def create_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    extra_properties: dict[str, object] | None = None,
    source: str = "manual",
) -> None:
    """node_key 创建时直接取 standard_name 的值（Global Constraints 的
    node_key 生成规则：extraction 模式下没有外部稳定码来源）。

    source 记录这条术语最初是通过哪个渠道创建的（manual/etl/review），
    默认值 "manual" 只是为了不用逐个改动测试里大量既有的 create_term()
    调用——本计划里唯一真正的生产调用点是 admin_terms_routes.py 的
    create_new_term，它现在只会被"知识图谱审核"页的内联创建调用，会显式
    传 source="review"。见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 C。
    """
    extra_properties = extra_properties or {}
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type,
        extra_properties=extra_properties,
    )
    await _check_name_conflict(conn, tenant_id=tenant_id, standard_name=standard_name, aliases=aliases)
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                standard_name,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                json.dumps(extra_properties, ensure_ascii=False),
                source,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{standard_name!r} 已经是已有术语的标准名，不能重复创建")
    await conn.commit()
```

`update_term`：同样删掉 `product_line: str,` 参数、`_validate_categories` 调用里的 `product_line=product_line,`、UPDATE 语句和参数元组里的 `product_line`：

```python
async def update_term(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    standard_name: str,
    new_standard_name: str,
    aliases: list[str],
    term_type: str,
    extra_properties: dict[str, object] | None = None,
) -> None:
    """standard_name 是当前（改名前）的名字，用来定位这条记录；
    new_standard_name 是提交的新名字，允许和 standard_name 相同（即不改名）。
    node_key 不受影响，UPDATE 语句不写这一列——ADR-0003 的核心断言：
    身份键创建后永不改变，即使术语被改名。

    UPDATE 语句不写 source 列——这是刻意的：source 只记录创建时的渠道，
    人工编辑（无论改名、改别名还是改属性）都不改变它，见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 C.4。
    """
    extra_properties = extra_properties or {}
    existing_term = await get_term(conn, tenant_id, standard_name)
    await _validate_categories(
        conn, tenant_id=tenant_id, term_type=term_type,
        extra_properties=extra_properties,
        existing_extra_property_keys=frozenset(existing_term.extra_properties),
    )
    await _check_name_conflict(
        conn, tenant_id=tenant_id, standard_name=new_standard_name, aliases=aliases,
        exclude_standard_name=standard_name,
    )
    try:
        await conn.execute(
            "UPDATE terms SET standard_name=?, aliases=?, term_type=?, "
            "extra_properties=? WHERE tenant_id=? AND node_key=?",
            (
                new_standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
                json.dumps(extra_properties, ensure_ascii=False),
                tenant_id,
                existing_term.node_key,
            ),
        )
    except aiosqlite.IntegrityError:
        raise TermNameConflictError(f"{new_standard_name!r} 已经是已有术语的标准名，不能重复使用")
    await conn.commit()
```

`upsert_term_with_node_key`：同样删掉 `product_line: str,` 参数、`_validate_categories` 调用里的 `product_line=product_line,`、INSERT/ON CONFLICT 语句和参数元组里的 `product_line`：

```python
async def upsert_term_with_node_key(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    extra_properties: dict[str, object] | None = None,
    source: str = "etl",
) -> None:
    """（docstring 其余部分不变，只是签名少了 product_line）"""
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
        conn, tenant_id=tenant_id, term_type=term_type,
        extra_properties=extra_properties,
        existing_extra_property_keys=existing_extra_property_keys,
    )
    try:
        await conn.execute(
            "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties, source) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, node_key) DO UPDATE SET "
            "standard_name = excluded.standard_name, aliases = excluded.aliases, "
            "term_type = excluded.term_type, "
            "extra_properties = excluded.extra_properties",
            (
                tenant_id,
                node_key,
                standard_name,
                json.dumps(aliases, ensure_ascii=False),
                term_type,
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

- [ ] **Step 12: 更新测试**

`tests/graphrag/test_terms_store.py`：这个文件有大量 `create_term`/`update_term`/`upsert_term_with_node_key`/`Term(...)` 调用都传了 `product_line` 关键字参数——grep 这个文件里 `product_line` 出现的每一处调用，删掉这个关键字参数（不是删测试本身，只是删参数）。如果有专门测试"未知产品线校验报错"的测试函数（比如断言 `UnknownCategoryError` 且消息含"未知产品线"），整个测试函数删除。新增一个测试验证 `ALTER TABLE ... DROP COLUMN` 迁移本身：

```python
async def test_ensure_terms_schema_drops_legacy_product_line_column(tmp_path):
    """模拟一个已经是 tenant_id 新结构、但还带着 product_line 列的老库（
    本次改造前的真实状态），验证 ensure_terms_schema 会把这一列原地删掉，
    且不影响其余数据。"""
    conn = await aiosqlite.connect(":memory:")  # 或本文件已有的建库 helper
    await conn.executescript(
        """
        CREATE TABLE terms (
            tenant_id TEXT NOT NULL, node_key TEXT NOT NULL,
            standard_name TEXT NOT NULL, aliases TEXT NOT NULL,
            term_type TEXT NOT NULL, product_line TEXT NOT NULL,
            extra_properties TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, node_key)
        );
        """
    )
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "product_line, extra_properties) VALUES ('t1', 'k1', 'n1', '[]', 'tt', 'pl', '{}')"
    )
    await conn.commit()

    await ensure_terms_schema(conn)

    cursor = await conn.execute("PRAGMA table_info(terms)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "product_line" not in columns
    term = await get_term(conn, "t1", "n1")
    assert term.standard_name == "n1"
```

`tests/graphrag/test_ontology.py`：`load_terminology`/`Term(...)` 相关测试里删掉 `product_line` 参数/断言。

- [ ] **Step 13: 运行测试**

Run: `python -m pytest tests/graphrag/test_terms_store.py tests/graphrag/test_ontology.py -v`
Expected: 全部 PASS，包括新增的 DROP COLUMN 迁移测试。

- [ ] **Step 14: Commit**

```bash
git add app/graphrag/ontology.py app/graphrag/terminology_seed.yaml app/graphrag/terms_store.py tests/graphrag/test_terms_store.py tests/graphrag/test_ontology.py
git commit -m "feat: drop product_line from Term dataclass and terms table"
```

---

## Task 3: `admin_terms_routes.py` API 层去掉 product_line

**Files:**
- Modify: `app/api/admin_terms_routes.py`
- Test: `tests/api/test_admin_terms_routes.py`

**Interfaces:**
- Consumes：Task 2 的 `Term`（无 `product_line`）、`create_term`/`update_term`（无 `product_line` 参数）。
- Produces：`TermResponse`/`TermWriteRequest` 不再有 `product_line` 字段——这是前端 Task 8 依赖的破坏性变更。

- [ ] **Step 1: `TermResponse`/`TermWriteRequest` 删掉 product_line**

`app/api/admin_terms_routes.py` 第 33-39 行：

```python
class TermResponse(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    extra_properties: dict[str, Any] = {}
    source: str
```

第 47-53 行：

```python
class TermWriteRequest(BaseModel):
    standard_name: str
    aliases: list[str]
    term_type: str
    extra_properties: dict[str, Any] = {}
    source: Literal["manual", "etl", "review", "unknown"] = "manual"
```

第 65-71 行的 `field_validator("term_type", "product_line")` 改成只校验 `"term_type"`：

```python
    @field_validator("term_type")
    @classmethod
    def _validate_required_field(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped
```

- [ ] **Step 2: `_to_response` 和三处 `create_term`/`update_term`/`Term(...)` 调用删掉 product_line**

`_to_response`（第 79-87 行）删掉 `product_line=term.product_line,`。

`create_new_term`（第 117-164 行）：`create_term(...)` 调用（第 129-138 行）删掉 `product_line=payload.product_line,`；`Term(...)` 构造（第 145-154 行）删掉 `product_line=payload.product_line,`。

`update_existing_term`（第 167-236 行）：`update_term(...)` 调用（第 181-190 行）删掉 `product_line=payload.product_line,`；`Term(...)` 构造（第 218-227 行）删掉 `product_line=payload.product_line,`；第 202 行注释"再用 sync_term 刷新 type/product_line/别名"改成"再用 sync_term 刷新 type/别名"。

- [ ] **Step 3: 更新测试**

`tests/api/test_admin_terms_routes.py`：grep `product_line` 定位所有请求体构造/响应断言，删掉这个字段（请求体里删这个 key，响应断言如果是完整 dict 比较则同步删掉这个 key）。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/api/test_admin_terms_routes.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add app/api/admin_terms_routes.py tests/api/test_admin_terms_routes.py
git commit -m "feat: drop product_line from admin terms API"
```

---

## Task 4: `neo4j_client.py` 图谱同步去掉 product_line

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `app/ingestion/main.py`
- Test: `tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Consumes：Task 2 的 `Term`（无 `product_line` 字段，`term.product_line` 访问会报 `AttributeError`）。
- Produces：`sync_term`/`_SYNC_TERM_QUERY`/查询结果行不再涉及 `product_line`——图谱里历史节点的 `product_line` 属性不受影响（不清理，见 Global Constraints）。

- [ ] **Step 1: `_SYNC_TERM_QUERY` 去掉 product_line 赋值**

`app/graphrag/neo4j_client.py` 第 99-102 行：

```python
_SYNC_TERM_QUERY = """
MERGE (t:Term {tenant_id: $tenant_id, node_key: $node_key})
SET t.standard_name = $standard_name, t.type = $type
SET t += $extra_properties
```

（第 103 行起的 `WITH t` 及后续不变。）

- [ ] **Step 2: `sync_term` 方法删掉传参**

第 381-400 行：docstring（第 382-386 行）"写入/更新标准节点的 type/product_line 属性"改成"写入/更新标准节点的 type 属性"；`session.run` 的参数字典（第 391-399 行）删掉 `"product_line": term.product_line,`。

- [ ] **Step 3: 查询结果 RETURN 子句删掉 product_line**

第 274-281 行区域：

```python
        query = (
            "MATCH (anchor:Term {tenant_id: $tenant_id, type: $anchor_term_type}) "
            f"WHERE {where_sql} "
            "RETURN anchor.standard_name AS standard_name, anchor.node_key AS node_key, "
            "anchor.type AS term_type, "
            "properties(anchor) AS all_properties "
            "LIMIT $limit"
        )
```

- [ ] **Step 4: 顺手修正 `ingestion/main.py` 里一处已经不准确的注释**

`app/ingestion/main.py` 第 80-82 行的注释"术语表（基准真相）先同步进图谱：写入/更新标准节点的 type/product_line 属性 + 别名节点……"，删掉"/product_line"：

```python
        # 术语表（基准真相）先同步进图谱：写入/更新标准节点的 type
        # 属性 + 别名节点，再进入下面的文档摄取+关系抽取——保证图谱里不只有
        # LLM 抽取出的关系边，也有完整的实体+别名+分类信息（架构文档 §4.1）。
```

- [ ] **Step 5: 更新测试**

`tests/graphrag/test_neo4j_client.py`：grep `product_line`，删掉测试里构造 `Term(...)` 传的这个参数；如果有断言 Cypher 查询参数字典/返回行包含 `product_line` key 的用例，同步改成不包含。

- [ ] **Step 6: 运行测试**

Run: `python -m pytest tests/graphrag/test_neo4j_client.py -v`
Expected: 全部 PASS（这个文件的测试大概率是对着一个 fake/mock 的 Neo4j driver 断言 Cypher 查询字符串或参数字典，不需要真实 Neo4j 连接）。

- [ ] **Step 7: Commit**

```bash
git add app/graphrag/neo4j_client.py app/ingestion/main.py tests/graphrag/test_neo4j_client.py
git commit -m "feat: drop product_line from Neo4j term sync and subgraph query"
```

---

## Task 5: ETL 配置与写入引擎去掉 product_line

**Files:**
- Modify: `app/graphrag/schema_etl_config.py`
- Modify: `app/graphrag/schema_etl.py`
- Test: `tests/graphrag/test_schema_etl_config.py`
- Test: `tests/graphrag/test_schema_etl.py`
- Test: `tests/api/test_admin_schema_etl_routes.py`（如涉及）

**Interfaces:**
- Consumes：Task 2 的 `upsert_term_with_node_key`/`Term`（无 `product_line`）。
- Produces：`EntityMapping` 不再有 `product_line` 字段——ETL YAML 配置格式变更，不兼容旧文件（Global Constraints，已确认没有租户在实际使用）。

- [ ] **Step 1: `EntityMapping` dataclass 删掉 product_line**

`app/graphrag/schema_etl_config.py` 第 24-32 行：

```python
@dataclass(frozen=True)
class EntityMapping:
    term_type: str
    source_file: str
    standard_name_column: str
    node_key_parts: list[ColumnNodeKeyPart | AllocatedCodeNodeKeyPart]
    field_mappings: dict[str, str]
```

`_parse_entity_mapping`（第 68-86 行）删掉 `product_line=raw["product_line"],`（第 78 行）：

```python
        return EntityMapping(
            term_type=raw["term_type"],
            source_file=raw["source_file"],
            standard_name_column=raw["standard_name_column"],
            node_key_parts=[_parse_node_key_part(part) for part in node_key_parts_raw],
            field_mappings=dict(raw.get("field_mappings") or {}),
        )
```

- [ ] **Step 2: `schema_etl.py::_write_entity_mapping` 删掉 product_line 传参**

`app/graphrag/schema_etl.py` 第 80-130 行区域，`upsert_term_with_node_key(...)`（第 112-116 行）和 `Term(...)`（第 117-121 行）两处构造都删掉 `product_line=mapping.product_line,`：

```python
            await upsert_term_with_node_key(
                conn, tenant_id=tenant_id, node_key=node_key, standard_name=standard_name,
                aliases=[], term_type=mapping.term_type,
                extra_properties=extra_properties,
            )
            term = Term(
                tenant_id=tenant_id, node_key=node_key, standard_name=standard_name,
                aliases=[], term_type=mapping.term_type,
                extra_properties=extra_properties,
            )
```

- [ ] **Step 3: 更新测试和示例 YAML/CSV 配置**

`tests/graphrag/test_schema_etl_config.py`：grep `product_line`，删掉测试用的 YAML 字符串/dict 里的这个字段，以及对应的断言。

`tests/graphrag/test_schema_etl.py`：grep `product_line`，删掉 `EntityMapping(...)` 构造里的这个参数；`_confirmed_conn()` 或类似的建库 fixture 如果调用过 `create_product_line`（Task 1 已删除这个函数），要同步删掉那一行调用——检查这个文件顶部的 import，如果导入了 `create_product_line`，删掉这个 import。

`tests/api/test_admin_schema_etl_routes.py`：grep `product_line`，如果测试用的示例 YAML 配置字符串里有这个字段，删掉。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/graphrag/test_schema_etl_config.py tests/graphrag/test_schema_etl.py tests/api/test_admin_schema_etl_routes.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add app/graphrag/schema_etl_config.py app/graphrag/schema_etl.py tests/graphrag/test_schema_etl_config.py tests/graphrag/test_schema_etl.py tests/api/test_admin_schema_etl_routes.py
git commit -m "feat: drop product_line from ETL config schema and write engine"
```

---

## Task 6: 结构化检索与 LLM 上下文展示去掉 product_line

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`
- Modify: `app/graphrag/term_guard.py`
- Test: `tests/graphrag/test_structured_filter_query.py`
- Test: `tests/graphrag/test_term_guard.py`

**Interfaces:**
- Consumes：Task 2 的 `Term`（无 `product_line`）、Task 4 的查询结果行（不再含 `product_line` key）。
- Produces：`_CORE_TERM_FIELDS` 少一个成员；`run_structured_filter_query` 返回的每一行结果不再有 `product_line` key；LLM 检索上下文文本不再展示产品线。

- [ ] **Step 1: `_CORE_TERM_FIELDS` 去掉 `"product_line"`**

`app/graphrag/structured_filter_query.py` 第 260 行：

```python
_CORE_TERM_FIELDS = frozenset({"tenant_id", "node_key", "standard_name", "type"})
```

- [ ] **Step 2: 组装结果行删掉 product_line key**

`run_structured_filter_query` 里组装每一行结果字典的位置（第 303 行附近），删掉 `"product_line": row["product_line"],` 这一行（`row` 已经在 Task 4 里不再包含这个 key，本任务是把消费侧也同步摘掉，而不是留一个永远 `KeyError` 的死代码）。

- [ ] **Step 3: `term_guard.py` 展示文本去掉产品线**

第 90-93 行：

```python
        lines.append(
            f"- {term.standard_name}（类型: {term.term_type}）"
        )
```

- [ ] **Step 4: 更新测试**

`tests/graphrag/test_structured_filter_query.py`：grep `product_line`，删掉测试夹具 `Term(...)`/结果行构造里的这个字段；如果有断言查询结果行包含 `product_line` key 的用例，改成断言不包含（或直接删掉那条断言，视上下文而定）。

`tests/graphrag/test_term_guard.py`：grep `product_line`，删掉测试夹具里的这个字段；如果有断言展示文本里出现"产品线: xxx"的用例，改成断言文本不再包含"产品线"这几个字。

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/graphrag/test_structured_filter_query.py tests/graphrag/test_term_guard.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/structured_filter_query.py app/graphrag/term_guard.py tests/graphrag/test_structured_filter_query.py tests/graphrag/test_term_guard.py
git commit -m "feat: drop product_line from structured filter query and LLM context"
```

---

## Task 7: 前端「本体 Schema 管理」页删除产品线 tab

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`

**Interfaces:**
- Consumes：Task 1 已删除 `/api/admin/ontology/product-lines` 系列端点——本任务删除前端对它们的调用。
- Produces：无新增导出，页面组件本身。

- [ ] **Step 1: `Tab` 类型收窄，去掉产品线 tab 按钮和渲染分支**

`frontend/src/admin/OntologySchemaPage.tsx` 第 6 行：

```typescript
type Tab = 'term-types' | 'relation-types' | 'constraints'
```

第 319-328 行（"产品线" tab 按钮）整块删除。

第 364-366 行（`{tab === 'product-lines' && <ProductLinesTab ... />}`）整块删除。

- [ ] **Step 2: 删除 `ProductLinesTab` 组件整个函数**

第 1371 行到文件末尾（第 1511 行，即整个文件的最后一段）——`ProductLinesTab` 函数定义及其前面的分隔注释是文件里最后一个顶层声明，直接删到文件结尾。

- [ ] **Step 3: 手动验证**

启动前端开发服务器，打开本体 Schema 管理页：
1. 确认顶部只剩"实体类型"/"关系类型"/"约束"三个 tab，没有"产品线"。
2. 确认三个剩余 tab 切换、创建/编辑/删除功能不受影响。
3. 确认"确认 schema"按钮的前置条件判断（要求实体类型/关系类型草稿/约束草稿都非空）不受影响——这个逻辑本来就不看产品线。

- [ ] **Step 4: 运行测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/OntologySchemaPage.tsx
git commit -m "feat(frontend): remove product-line tab from ontology schema page"
```

---

## Task 8: 前端「实体列表」「非结构化数据加工」页删除产品线字段

**Files:**
- Modify: `frontend/src/admin/termsApi.ts`
- Modify: `frontend/src/admin/TermsPage.tsx`
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`

**Interfaces:**
- Consumes：Task 3 的 `TermResponse`/`TermWriteRequest`（无 `product_line`）。
- Produces：`TermRecord`（前端 TS 类型）不再有 `product_line: string`——这是一个必填字段的删除，任何手写 `TermRecord` 字面量的地方都要跟着改。

- [ ] **Step 1: `termsApi.ts` 删掉 `TermRecord.product_line`**

`frontend/src/admin/termsApi.ts` 第 8-12 行：

```typescript
export interface TermRecord extends GraphTerm {
  term_type: string
  source: string
}
```

- [ ] **Step 2: `TermsPage.tsx` 删掉产品线相关的一切**

`frontend/src/admin/TermsPage.tsx`：
- `TermDraft` 接口（第 15-20 行）删掉 `product_line: string`。
- `toDraft`（第 22-29 行）删掉 `product_line: term.product_line,`。
- `draftToRecord`（第 31-45 行）删掉 `product_line: draft.product_line.trim(),`。
- `productLineOptions` state（第 57 行）删除。
- 加载枚举的 `useEffect`（第 71-92 行）里，`Promise.all` 数组只保留 `term-types` 那一个 fetch，删掉 `product-lines` 那个 fetch 分支：

```typescript
  useEffect(() => {
    if (!sessionToken) return
    setOptionsLoaded(false)
    adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken)
      .then((res) => res.json())
      .then((data: { term_types: { value: string }[] }) =>
        setTermTypeOptions(data.term_types.map((t) => t.value)),
      )
      .catch((err) => {
        console.error('加载实体类型枚举失败', err)
        return null
      })
      .finally(() => setOptionsLoaded(true))
  }, [sessionToken, tenantId])
```

- 列表展示里的产品线文字（第 230-233 行区域）删掉 ` · {term.product_line || '（无产品线）'}` 这部分，只保留类型：

```tsx
                    <span className="text-ink-soft">
                      {' '}
                      · {term.term_type || '（无类型）'}
                    </span>
```

- 编辑表单里的产品线 `<select>`（第 303-322 行区域）整块删除。

- [ ] **Step 3: `GraphReviewsPage.tsx` 内联创建表单删掉产品线字段**

`frontend/src/admin/GraphReviewsPage.tsx`：
- `CreateEntityDraft` 接口（第 37-46 行）删掉 `productLine: string`。
- `productLineOptions` state（第 83 行）删除。
- 加载枚举的 `useEffect`（第 101-121 行）同样只保留 term-types 那一个 fetch（模式跟 Task 8 Step 2 的 `TermsPage.tsx` 相同）：

```typescript
  useEffect(() => {
    if (!sessionToken) return
    adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken)
      .then((res) => res.json())
      .then((data: { term_types: { value: string }[] }) =>
        setTermTypeOptions(data.term_types.map((t) => t.value)),
      )
      .catch((err) => {
        console.error('加载实体类型枚举失败', err)
        return null
      })
  }, [sessionToken, tenantId])
```

- `handleOpenCreateEntity` 里 `setCreateDraft({...})` 的初始值（第 426 行附近）删掉 `productLine: '',`。
- `handleSubmitCreateEntity` 的表单步骤校验（第 436 行）改成只看 `termType`：

```typescript
      if (!createDraft.termType) return
```

- `handleSubmitCreateEntity` 提交给 `createTerm(...)` 的对象（第 447 行附近）删掉 `product_line: createDraft.productLine,`。
- 表单 JSX 里的产品线 `<label>`/`<select>`（第 569-584 行区域）整块删除。
- "下一步"按钮的 `disabled` 条件（第 589 行）改成只看 `!createDraft.termType`。
- 确认步骤展示文本（第 614 行）删掉 `<br />产品线：{createDraft.productLine}` 这一行。

- [ ] **Step 4: 手动验证**

启动前后端开发服务器：
1. 打开「实体列表」页，确认编辑表单不再有产品线下拉，列表展示不再显示产品线文字。
2. 打开「非结构化数据加工」页，触发内联创建实体，确认表单只有"实体类型"一个下拉、二次确认框不再展示产品线、提交成功。

- [ ] **Step 5: 运行测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/admin/termsApi.ts frontend/src/admin/TermsPage.tsx frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(frontend): remove product-line field from entity list and inline creation"
```

---

## Task 9: 扫尾——其余测试文件的夹具同步改 + 全量验证

**Files:**
- Modify（视 grep 结果而定，预期涉及 tests/graphrag、tests/api、tests/ingestion、tests/agent、tests/qa、tests/voice、tests/eval 目录下的多个文件）

**Interfaces:**
- Consumes：Task 1-8 全部落地后的最终状态。
- Produces：全量测试套件、`tsc --noEmit` 均通过。

- [ ] **Step 1: 全文搜索确认没有代码引用残留**

```bash
grep -rn "product_line\|productLine\|产品线" app/ frontend/src/ --include="*.py" --include="*.ts" --include="*.tsx"
```

Expected: 空结果（如果不是空，说明 Task 1-8 有遗漏，回去补）。

- [ ] **Step 2: 扫描并修复剩余测试文件**

```bash
grep -rln "product_line" tests/
```

对每一个还出现 `product_line` 的文件（预期主要是纯粹传参数的夹具代码，不是被测试的行为本身），删掉 `Term(...)`/`create_term(...)`/`update_term(...)`/`upsert_term_with_node_key(...)`/`EntityMapping(...)`/`TermWriteRequest`/`TermRecord` 等构造调用里的 `product_line`/`product_line=...` 关键字参数或 dict key。这是同一种小改动在多个文件里重复，不需要逐个深入设计，但每个文件改完后要过一遍 diff 确认没有误删无关内容。预期涉及的文件（以 grep 实际结果为准，这里列出的是写 spec 时已知会用到 `product_line` 作为夹具参数的文件）：

`tests/graphrag/test_review_queue.py`、`tests/graphrag/test_review_cli.py`、`tests/graphrag/test_normalization.py`、`tests/api/test_admin_graph_review_routes.py`、`tests/ingestion/test_ingest_main.py`、`tests/ingestion/test_ingest_pipeline.py`、`tests/ingestion/test_graph_extraction.py`、`tests/api/test_admin_document_routes.py`、`tests/graphrag/test_term_matcher.py`、`tests/agent/test_tools.py`、`tests/agent/test_planner.py`、`tests/agent/test_graph_planner.py`、`tests/agent/test_graph.py`、`tests/qa/test_answer.py`、`tests/api/test_voice_finalize_routes.py`、`tests/voice/test_asr_term_correction.py`、`tests/eval/test_terminology_accuracy.py`、`tests/graphrag/test_graph_factory.py`。

- [ ] **Step 3: 全量后端测试**

Run: `python -m pytest tests/ -q`（synchronously，不要 backgrounding 等通知——见本环境已知的 aiosqlite 退出清理卡顿问题，测试本身跑完后进程可能不会立刻退出，等它完成或在确认测试结果已经打印后手动结束进程）
Expected: 除了已知的、与本次改动无关的 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`（本机 `.env` 配置了真实 TTS 凭据导致的环境相关失败）之外全部 PASS。

- [ ] **Step 4: 前端类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误。

- [ ] **Step 5: 用一份真实 ETL YAML 跑一遍验证新 schema**

用不含 `product_line` 字段的示例 `EntityMapping` YAML（`tests/graphrag/test_schema_etl.py` 里已有的测试夹具就是现成的例子）确认 `run_schema_etl` 能正常解析、写入，不报 `KeyError`/`InvalidSchemaETLConfigError`——这一步如果 Task 5 的测试已经覆盖，此处可以直接引用那次测试结果，不需要重复手动跑。

- [ ] **Step 6: Commit（如果 Step 2 有改动）**

```bash
git add tests/
git commit -m "test: sync remaining test fixtures after product_line removal"
```

---

## 最终验证（写给执行本计划的 controller，不是单独一个 task）

全部 9 个 Task 完成后，在进入 `superpowers:subagent-driven-development` 的最终整体 review 之前，controller 自己应确认：

- `python -m pytest tests/ -q` 全量通过（除已知无关的 TTS 环境测试）。
- `cd frontend && npx tsc --noEmit` 无类型错误。
- 对照 spec 文档的"验收标准"一节逐条核对：`product_line` 全文无残留引用（Neo4j 历史属性除外）、`terms`/`ontology_product_lines` 表结构、本体 Schema 管理页 3 个 tab、ETL YAML 新 schema。
