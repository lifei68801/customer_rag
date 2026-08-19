# 实体类型接入草稿/已确认生命周期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"实体类型"（`ontology_term_types`）跟"关系类型"（`tenant_relation_types`）/"约束"（`term_type_relation_allowlist`）一样接入真正的草稿/已确认两态生命周期，替换掉当前"新增/修改/删除立刻生效、没有草稿"的行为。

**Architecture:** 复制关系类型现有的 `status` 列 + 复合主键模式（`(tenant_id, value, status)`），把 `create/update/delete_term_type` 收窄到只操作草稿行，`checkout_draft`/`confirm_ontology` 统一纳管这张表；真实术语只能引用已确认类型（`terms_store.py::_validate_categories` 改查 `confirmed`）；约束的类型引用改查 `draft`（跟关系类型引用现有逻辑对齐）；改名/删除已确认类型不再自动级联到真实术语，改为一个新的"迁移实体类型"工具（同时改 SQLite `terms` 表和 Neo4j `:Term` 节点属性）。

**Tech Stack:** FastAPI + aiosqlite + Neo4j（后端），React + TypeScript（前端）。

**Spec:** docs/superpowers/specs/2026-08-19-term-type-draft-lifecycle-design.md

## Global Constraints

- `list_term_types()` 的 `status` 参数改成必填关键字参数（不给默认值）——强迫每个调用点显式声明，任何遗漏都会在 `pytest`/`tsc` 阶段直接报错，不会静默用错状态。
- `is_ontology_confirmed()` **不改**，继续只检查 `tenant_relation_types` 是否有已确认行——"三者都要有草稿才能确认"目前只是前端 UI 层的把关，不是后端硬约束（详见 spec 文档"决策摘要"下方关于 `is_ontology_confirmed` 的说明），把它做成后端硬约束不在本次范围内。
- 改名/删除一个**已确认**的实体类型，不再级联更新真实术语（`terms` 表）——这是本次唯一一处删掉现有级联行为的地方，级联改用新的"迁移实体类型"工具手动触发。
- 每个改动的后端函数/路由都要有对应 pytest 覆盖；每个前端改动都要过 `npx tsc --noEmit`。

---

### Task 1: `ontology_categories.py` 接入 status 列

**Files:**
- Modify: `app/graphrag/ontology_categories.py`
- Test: `tests/graphrag/test_ontology_categories.py`（现有 12 处 `list_term_types(...)` 调用点全部需要补 `status` 参数，且不少测试的断言逻辑要跟着"create 进草稿、不进已确认"这个新语义重写）

**Interfaces:**
- Produces（后续任务依赖这些确切签名）：
  - `async def list_term_types(conn, tenant_id, *, status: str) -> list[TermTypeCategory]`（`status` 必填）
  - `async def create_term_type(conn, tenant_id, *, value, extra_fields=None) -> None`（签名不变，内部固定写 `status='draft'`）
  - `async def update_term_type(conn, tenant_id, *, value, new_value, extra_fields) -> None`（签名不变，只操作/查找 `status='draft'` 的行；找不到草稿行抛 `CategoryNotFoundError`；改名级联范围收窄为只更新 `term_type_relation_allowlist` 的 draft 行，不再动 `terms` 表）
  - `async def delete_term_type(conn, tenant_id, value) -> None`（签名不变，只删 `status='draft'` 的行；引用检查：`terms` 表引用检查范围不变，`term_type_relation_allowlist` 引用检查加 `AND status = 'draft'`）

- [ ] **Step 1: 表结构 + 迁移**

`app/graphrag/ontology_categories.py` 的 `_SCHEMA_SQL` 改成：

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
CREATE TABLE IF NOT EXISTS ontology_product_lines (
    value TEXT PRIMARY KEY
);
"""
```

新增迁移函数，紧跟在现有 `_migrate_term_types_table_if_needed` 函数定义之后：

```python
async def _migrate_term_types_add_status_if_needed(conn: aiosqlite.Connection) -> None:
    """把 2026-08-19 之前没有 status 列的 ontology_term_types 表（PK 是
    (tenant_id, value)，所有行隐含"直接生效"）迁移成带草稿/已确认两态的
    新结构（PK 变成 (tenant_id, value, status)）。存量行全部落
    status='confirmed'——迁移前它们本来就是"当前生效"的状态，语义上对应
    新模型里的已确认，不是草稿。跟 _migrate_term_types_table_if_needed
    同构，必须排在它之后调用（那个函数先保证 tenant_id 列存在，这个函数
    的 SELECT 依赖 tenant_id 列已经在）。
    """
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_term_types'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await conn.execute("PRAGMA table_info(ontology_term_types)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if "status" in existing_columns:
        return
    await conn.executescript(
        """
        CREATE TABLE ontology_term_types_new (
            tenant_id         TEXT NOT NULL,
            value             TEXT NOT NULL,
            extra_fields      TEXT NOT NULL DEFAULT '[]',
            node_key_template TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL,
            PRIMARY KEY (tenant_id, value, status)
        );
        """
    )
    await conn.execute(
        "INSERT INTO ontology_term_types_new "
        "(tenant_id, value, extra_fields, node_key_template, status) "
        "SELECT tenant_id, value, extra_fields, node_key_template, 'confirmed' "
        "FROM ontology_term_types"
    )
    await conn.executescript(
        "DROP TABLE ontology_term_types; "
        "ALTER TABLE ontology_term_types_new RENAME TO ontology_term_types;"
    )
    await conn.commit()
```

`ensure_categories_schema` 里在 `_migrate_term_types_table_if_needed` 之后、`_SCHEMA_SQL` 之前插入这一步：

```python
async def ensure_categories_schema(conn: aiosqlite.Connection) -> None:
    await _migrate_term_types_table_if_needed(conn)
    await _migrate_term_types_add_status_if_needed(conn)
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()
    await _migrate_extra_fields_value_shape_if_needed(conn)
```

- [ ] **Step 2: `list_term_types` 加必填 status 参数**

```python
async def list_term_types(
    conn: aiosqlite.Connection, tenant_id: str, *, status: str
) -> list[TermTypeCategory]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT value, extra_fields FROM ontology_term_types "
        "WHERE tenant_id = ? AND status = ? ORDER BY value",
        (tenant_id, status),
    )
    rows = await cursor.fetchall()
    return [_row_to_term_type(row) for row in rows]
```

- [ ] **Step 3: `create_term_type` 固定写 draft**

```python
async def create_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    extra_fields: list[ExtraFieldSpec] | None = None,
) -> None:
    extra_fields = extra_fields or []
    _validate_extra_field_specs(extra_fields)
    try:
        await conn.execute(
            "INSERT INTO ontology_term_types (tenant_id, value, extra_fields, status) "
            "VALUES (?, ?, ?, 'draft')",
            (tenant_id, value, _extra_fields_to_json(extra_fields)),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{value!r} 已经是该租户草稿里的分类，不能重复创建")
    await conn.commit()
```

- [ ] **Step 4: `update_term_type` 收窄到草稿、不再级联 `terms` 表**

```python
async def update_term_type(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    value: str,
    new_value: str,
    extra_fields: list[ExtraFieldSpec],
) -> None:
    """value 是草稿里的当前名字，new_value 是提交的新名字，允许相同（即不
    改名）。改名只级联更新该租户草稿约束表（term_type_relation_allowlist）
    里引用旧名字的 draft 行——不再级联更新 terms 表（真实术语只引用已确认
    类型，改草稿定义不影响它们；已确认类型改名后要同步真实术语，用新的
    "迁移实体类型"工具手动触发，见 terms_store.py::migrate_term_type）。
    """
    _validate_extra_field_specs(extra_fields)
    cursor = await conn.execute(
        "SELECT 1 FROM ontology_term_types WHERE tenant_id = ? AND value = ? AND status = 'draft'",
        (tenant_id, value),
    )
    if await cursor.fetchone() is None:
        raise CategoryNotFoundError(f"草稿里不存在分类: {value}")
    try:
        await conn.execute(
            "UPDATE ontology_term_types SET value = ?, extra_fields = ? "
            "WHERE tenant_id = ? AND value = ? AND status = 'draft'",
            (new_value, _extra_fields_to_json(extra_fields), tenant_id, value),
        )
    except aiosqlite.IntegrityError:
        raise CategoryNameConflictError(f"{new_value!r} 已经是该租户草稿里的分类，不能重复使用")
    if new_value != value:
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET subject_term_type = ? "
            "WHERE tenant_id = ? AND subject_term_type = ? AND status = 'draft'",
            (new_value, tenant_id, value),
        )
        await conn.execute(
            "UPDATE OR IGNORE term_type_relation_allowlist SET object_term_type = ? "
            "WHERE tenant_id = ? AND object_term_type = ? AND status = 'draft'",
            (new_value, tenant_id, value),
        )
    await conn.commit()
```

- [ ] **Step 5: `delete_term_type` 收窄到草稿**

```python
async def delete_term_type(conn: aiosqlite.Connection, tenant_id: str, value: str) -> None:
    """terms 表引用检查范围不变（真实术语只引用已确认类型，这个检查天然
    对应"已确认版本是否在用"）；term_type_relation_allowlist 引用检查加
    status='draft'——只拦"删除会破坏当前草稿自洽性"的情况，跟这次删除无关
    的已确认约束不受影响。"""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM terms WHERE tenant_id = ? AND term_type = ?", (tenant_id, value)
    )
    terms_count = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM term_type_relation_allowlist "
        "WHERE tenant_id = ? AND status = 'draft' AND (subject_term_type = ? OR object_term_type = ?)",
        (tenant_id, value, value),
    )
    allowlist_count = (await cursor.fetchone())[0]
    if terms_count > 0 or allowlist_count > 0:
        raise CategoryInUseError(
            f"分类 {value!r} 仍被 {terms_count} 条术语、{allowlist_count} 条关系约束引用，无法删除"
        )
    await conn.execute(
        "DELETE FROM ontology_term_types WHERE tenant_id = ? AND value = ? AND status = 'draft'",
        (tenant_id, value),
    )
    await conn.commit()
```

- [ ] **Step 6: 重写 `tests/graphrag/test_ontology_categories.py`**

这个文件目前的每一个测试都假设"没有草稿/已确认之分"，需要逐个检查、按新语义重写。**参照 `tests/graphrag/test_ontology_relations.py` 的现有测试结构**（那个文件测的是 `tenant_relation_types`，跟这次要做的 `ontology_term_types` 是完全同构的表设计，`list_relation_types(conn, tenant_id, status="draft")` 的用法就是 `list_term_types` 现在要照抄的调用方式）。具体要求：

1. 所有 `list_term_types(conn, tenant_id=...)` 调用点补上 `status="draft"` 或 `status="confirmed"`（按测试场景决定：测"create 完能查到什么"，一般是 `status="draft"`，因为 create 现在只进草稿）。
2. 新增至少一个测试验证"create 之后草稿列表能看到、已确认列表看不到"（对照 `test_ontology_relations.py::test_create_relation_type_with_valid_name` 的模式）。
3. `test_update_term_type_renames_without_referencing_terms`（原有测试，名字可能因为语义变化需要调整）：改成验证"改名只改草稿定义，不影响 terms 表"——即找到现有那个测"改名级联到 terms 表"的测试，把断言反过来：改名后 `terms` 表里的 `term_type` 值**不变**（因为不再级联）。参照 spec 文档"决策 4"。
4. `test_update_term_type_cascades_rename_to_referencing_terms`（如果现有文件里有类似名字的测试）：这类测试的原有断言（改名后 terms 表级联更新）现在是**错误行为**，必须删除或改写成验证"不级联"的反向断言，不能保留旧断言。
5. 新增删除保护测试：验证删除草稿中的实体类型时，如果 `terms` 表里有真实术语引用它（模拟"已确认版本在用"的场景），删除被 `CategoryInUseError` 拦住。
6. 新增测试验证 `delete_term_type` 只删 `status='draft'` 的行，不影响同名的 `status='confirmed'` 行（如果两者同时存在于该 value 上）。
7. 新增测试覆盖 `_migrate_term_types_add_status_if_needed`：手工建一张没有 `status` 列的旧表（`executescript` 直接建表插入几行），调用 `ensure_categories_schema`，断言迁移后所有行 `status='confirmed'`。参照现有文件里 `test_ensure_categories_schema_migrates_legacy_term_types_table` 那个测试的手法（模拟旧表结构、调用 ensure、断言迁移结果）。
8. 通读现有文件剩余的每一个测试函数，凡是调用了 `list_term_types`/`create_term_type`/`update_term_type`/`delete_term_type` 的，都要过一遍确认新语义下断言依然成立，不成立的要改。不要漏掉任何一个——用 `grep -n "async def test_" tests/graphrag/test_ontology_categories.py` 先拉一份完整清单，逐个确认。

- [ ] **Step 7: 跑测试**

```bash
python -m pytest tests/graphrag/test_ontology_categories.py -q
```
Expected: 全部通过。

- [ ] **Step 8: Commit**

```bash
git add app/graphrag/ontology_categories.py tests/graphrag/test_ontology_categories.py
git commit -m "feat(graphrag): give term types a draft/confirmed lifecycle like relation types"
```

---

### Task 2: `ontology_lifecycle.py` 纳管实体类型

**Files:**
- Modify: `app/graphrag/ontology_lifecycle.py`
- Test: `tests/graphrag/test_ontology_lifecycle.py`

**Interfaces:**
- Consumes：Task 1 的 `list_term_types(conn, tenant_id, *, status)`、`create_term_type`（都已存在，签名从 Task 1 起变化）。

- [ ] **Step 1: `_TABLES_WITH_TENANT_LIFECYCLE` 加入实体类型表**

```python
_TABLES_WITH_TENANT_LIFECYCLE = (
    "tenant_relation_types", "term_type_relation_allowlist", "ontology_term_types",
)
```

- [ ] **Step 2: `checkout_draft` 加第三段**

在现有 `checkout_draft` 函数体末尾（`await conn.commit()` 之前）加：

```python
    if not await _has_any_row(conn, "ontology_term_types", tenant_id, "draft"):
        if await _has_any_row(conn, "ontology_term_types", tenant_id, "confirmed"):
            await conn.execute(
                "INSERT INTO ontology_term_types "
                "(tenant_id, value, extra_fields, node_key_template, status) "
                "SELECT tenant_id, value, extra_fields, node_key_template, 'draft' "
                "FROM ontology_term_types WHERE tenant_id = ? AND status = 'confirmed'",
                (tenant_id,),
            )
        # 新租户没有默认实体类型可播种——不同于关系类型有 10 种通用拓扑
        # 关系兜底，实体类型完全依赖业务定义，没有"合理默认值"这回事，
        # 两种接入模式（extraction/etl）在这一点上没有区别。
```

- [ ] **Step 3: `confirm_ontology` 的空草稿检测加入实体类型**

```python
    has_draft_in_any_table = (
        await _has_any_row(conn, "tenant_relation_types", tenant_id, "draft")
        or await _has_any_row(conn, "term_type_relation_allowlist", tenant_id, "draft")
        or await _has_any_row(conn, "ontology_term_types", tenant_id, "draft")
    )
```

（`for table in _TABLES_WITH_TENANT_LIFECYCLE:` 那个循环不用改——它已经是遍历 `_TABLES_WITH_TENANT_LIFECYCLE` 常量，Step 1 加完表名后循环自动纳管新表。）

`is_ontology_confirmed` **不改**——见本计划 Global Constraints 和 spec 文档的说明。

- [ ] **Step 4: 补测试**

在 `tests/graphrag/test_ontology_lifecycle.py` 里新增（照抄现有测试对 `tenant_relation_types`/`term_type_relation_allowlist` 的测法，改成测 `ontology_term_types`，用 Task 1 的 `create_term_type`/`list_term_types(status=...)`）：

1. `test_checkout_draft_copies_confirmed_term_types_into_new_draft`：`checkout_draft` → `create_term_type` → `confirm_ontology` → 再 `checkout_draft` → 断言新草稿里能看到这个类型（照抄 `test_checkout_draft_after_confirm_copies_confirmed_into_new_draft` 对关系类型的测法）。
2. `test_checkout_draft_does_not_seed_default_term_types_for_brand_new_tenant`：全新租户 `checkout_draft` 后，`list_term_types(conn, tenant_id, status="draft")` 应为空列表（跟关系类型不一样，没有默认值）。
3. `test_confirm_ontology_promotes_term_types_too`：`checkout_draft` → `create_term_type` → `confirm_ontology` → 断言 `list_term_types(conn, tenant_id, status="confirmed")` 能看到。
4. `test_confirm_ontology_is_idempotent_no_op_without_any_draft`：确认现有的"无草稿时 confirm 是 no-op"测试逻辑，在实体类型草稿也为空时依然成立（现有 `test_confirm_ontology_is_idempotent_no_op_without_draft` 测试很可能已经覆盖到这个路径，跑一遍确认没有因为新增的 `ontology_term_types` 检测分支而回归）。
5. 确认现有的 `test_confirm_ontology_promotes_constraints_too` 测试（已经会创建 `create_term_type`）在改动后依然通过——不需要新增，只需要跑一遍验证。

- [ ] **Step 5: 跑测试**

```bash
python -m pytest tests/graphrag/test_ontology_lifecycle.py -q
```
Expected: 全部通过。

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/ontology_lifecycle.py tests/graphrag/test_ontology_lifecycle.py
git commit -m "feat(graphrag): fold term types into the checkout/confirm draft lifecycle"
```

---

### Task 3: 更新全部 `list_term_types` 调用点 + 新增迁移函数

**Files:**
- Modify: `app/graphrag/ontology_constraints.py`
- Modify: `app/graphrag/terms_store.py`
- Modify: `app/graphrag/schema_etl.py`
- Modify: `app/api/deps.py`
- Test: `tests/graphrag/test_ontology_constraints.py`、`tests/graphrag/test_terms_store.py`、`tests/graphrag/test_schema_etl.py`（已确认这是唯一覆盖 `_write_entity_mapping` 的 ETL 测试文件）

**Interfaces:**
- Consumes：Task 1 的 `list_term_types(conn, tenant_id, *, status)`。
- Produces：`app/graphrag/terms_store.py::async def migrate_term_type(conn, tenant_id, *, old_type, new_type) -> int`（Task 4 依赖这个函数）。

- [ ] **Step 1: `ontology_constraints.py` — 约束校验实体类型改查草稿**

`_validate_references` 函数里：

```python
    known_types = {c.value for c in await list_term_types(conn, tenant_id)}
```

改成：

```python
    known_types = {c.value for c in await list_term_types(conn, tenant_id, status="draft")}
```

（这一行紧跟着的关系类型校验已经是 `status="draft"`，这里改完两者口径一致，注释已经说明了"约束条目必须与实体类型在同一草稿编辑会话中创建"的理由，不用再加注释。）

- [ ] **Step 2: `terms_store.py` — 真实术语校验改查已确认**

`_validate_categories` 函数里：

```python
    types = await list_term_types(conn, tenant_id)
```

改成：

```python
    types = await list_term_types(conn, tenant_id, status="confirmed")
```

`_bridge_seed_categories_from_existing_terms` 函数里：

```python
    known_types = await list_term_types(conn, tenant_id)
```

改成：

```python
    known_types = await list_term_types(conn, tenant_id, status="confirmed")
```

同一个函数下面的：

```python
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types (tenant_id, value, extra_fields) VALUES (?, ?, '[]')",
            (tenant_id, value),
        )
```

改成（加 status 列，写死 confirmed——这段是从历史 `terms` 表数据反向种分类，种出来的类型代表"已经在用"）：

```python
    for value in distinct_types:
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_term_types (tenant_id, value, extra_fields, status) "
            "VALUES (?, ?, '[]', 'confirmed')",
            (tenant_id, value),
        )
```

- [ ] **Step 3: `terms_store.py` — 新增 `migrate_term_type`**

加在文件里跟 `terms` 表相关的函数附近（比如 `delete_term`/`update_term` 一带）：

```python
async def migrate_term_type(
    conn: aiosqlite.Connection, tenant_id: str, *, old_type: str, new_type: str
) -> int:
    """把该租户 terms 表里 term_type 从旧值批量改成新值，返回受影响的行数。
    供"迁移实体类型"工具用——改名一个已确认的实体类型不会自动级联到这张
    表（见 ontology_categories.py::update_term_type 的说明），需要业务显式
    触发这个函数才会同步。
    """
    cursor = await conn.execute(
        "UPDATE terms SET term_type = ? WHERE tenant_id = ? AND term_type = ?",
        (new_type, tenant_id, old_type),
    )
    await conn.commit()
    return cursor.rowcount
```

- [ ] **Step 4: `schema_etl.py` — ETL 写入校验改查已确认**

`_write_entity_mapping` 函数里：

```python
    term_types = await list_term_types(conn, tenant_id)
```

改成：

```python
    term_types = await list_term_types(conn, tenant_id, status="confirmed")
```

- [ ] **Step 5: `deps.py` — 结构化过滤查询工具改查已确认**

`get_term_type_schema` 函数里：

```python
    categories = await list_term_types(review_conn, tenant_id)
```

改成：

```python
    categories = await list_term_types(review_conn, tenant_id, status="confirmed")
```

- [ ] **Step 6: 补测试**

- `tests/graphrag/test_ontology_constraints.py`：现有测试大多数应该不需要改（它们本来就是 `create_term_type` 后立刻 `add_allowed_combination`，创建的类型天然是 draft，校验也改成查 draft，行为不变）——跑一遍确认全绿。新增一个测试：创建一个**已确认**的实体类型（`create_term_type` + `confirm_ontology`，不额外 `checkout_draft` 出新草稿），验证在没有对应草稿类型的情况下 `add_allowed_combination` 报 `UnknownCategoryError`（约束只认草稿类型，不认已确认类型，即使已确认类型客观存在）。
- `tests/graphrag/test_terms_store.py`：找到测 `_validate_categories`/`create_term`/`update_term` 校验 term_type 合法性的现有测试，确认它们创建测试用类型时用的是 `create_term_type`（现在默认进草稿）——如果是，这些测试在改动后会全部失败（因为真实术语现在只认已确认类型），需要在这些测试里补一步 `confirm_ontology`（先 `checkout_draft` → `create_term_type` → `confirm_ontology`）才能让后续的 `create_term`/`update_term` 校验通过。新增 `tests/graphrag/test_terms_store.py` 里 `migrate_term_type` 的测试：创建几条 `term_type='旧类型'` 的术语，调用 `migrate_term_type(conn, tenant_id, old_type='旧类型', new_type='新类型')`，断言返回值等于受影响行数、且 `terms` 表里对应行的 `term_type` 确实变成新值；再测一次没有匹配行时返回 0。
- `tests/graphrag/test_schema_etl.py`（已确认这是唯一覆盖 `_write_entity_mapping` 和 `create_term_type` 的 ETL 测试文件）：找到里面调用 `create_term_type` 建测试用实体类型的地方，补上 `confirm_ontology`（先 `checkout_draft` → `create_term_type` → `confirm_ontology`），否则 ETL 校验会因为找不到"已确认"类型而全部失败。

- [ ] **Step 7: 跑测试**

```bash
python -m pytest tests/graphrag/test_ontology_constraints.py tests/graphrag/test_terms_store.py tests/graphrag/test_schema_etl.py -q
```
Expected: 全部通过。

- [ ] **Step 8: Commit**

```bash
git add app/graphrag/ontology_constraints.py app/graphrag/terms_store.py app/graphrag/schema_etl.py app/api/deps.py tests/graphrag/test_ontology_constraints.py tests/graphrag/test_terms_store.py
git commit -m "feat(graphrag): scope term-type validation to draft or confirmed per call site"
```

（如果 ETL 测试文件也有改动，一并加进这次 commit。）

---

### Task 4: Neo4j 迁移方法 + 管理 API

**Files:**
- Modify: `app/graphrag/neo4j_client.py`
- Modify: `app/api/admin_ontology_routes.py`
- Test: `tests/graphrag/test_neo4j_client.py`、`tests/api/test_admin_ontology_routes.py`

**Interfaces:**
- Consumes：Task 3 的 `terms_store.migrate_term_type(conn, tenant_id, *, old_type, new_type) -> int`。
- Produces：
  - `Neo4jGraphClient.migrate_term_type_nodes(self, *, tenant_id, old_type, new_type) -> int`
  - `GET /api/admin/ontology/{tenant_id}/term-types?status=draft`（新增 query 参数，默认 `"draft"`）
  - `POST /api/admin/ontology/{tenant_id}/term-types/migrate` body `{"old_type": str, "new_type": str}` → `{"terms_migrated": int, "graph_nodes_migrated": int}`

- [ ] **Step 1: `neo4j_client.py` 新增迁移方法**

加在 `migrate_relation_type_edges` 方法附近：

```python
    async def migrate_term_type_nodes(
        self, *, tenant_id: str, old_type: str, new_type: str
    ) -> int:
        """把某个租户所有旧 term_type 的 :Term 节点属性批量改成新值，返回
        迁移的节点数。term_type 是参数化传入的节点属性值（t.type），不是
        像关系类型那样拼进 Cypher 结构本身——不需要 migrate_relation_type_edges
        那套正则白名单校验防注入，也不需要"建新边、复制属性、删旧边"的
        重建套路，原地 SET 一下就行。
        """
        query = (
            "MATCH (t:Term {tenant_id: $tenant_id, type: $old_type}) "
            "SET t.type = $new_type "
            "RETURN count(t) AS migrated_count"
        )
        async with self._driver.session() as session:
            result = await session.run(
                query, {"tenant_id": tenant_id, "old_type": old_type, "new_type": new_type}
            )
            rows = await result.data()
        return rows[0]["migrated_count"] if rows else 0
```

- [ ] **Step 2: `admin_ontology_routes.py` — GET 加 status 参数**

`list_term_type_categories` 函数：

```python
@router.get("/{tenant_id}/term-types")
async def list_term_type_categories(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn, tenant_id)
```

改成：

```python
@router.get("/{tenant_id}/term-types")
async def list_term_type_categories(
    tenant_id: str,
    status: str = "draft",
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    result = await list_term_types(review_conn, tenant_id, status=status)
```

（其余不变，返回体结构不变。）

- [ ] **Step 3: `admin_ontology_routes.py` — 新增迁移路由**

在 `delete_term_type_category` 路由之后加：

```python
class MigrateTermTypeRequest(BaseModel):
    old_type: str
    new_type: str


class MigrateTermTypeResponse(BaseModel):
    terms_migrated: int
    graph_nodes_migrated: int


@router.post("/{tenant_id}/term-types/migrate")
async def migrate_tenant_term_type(
    tenant_id: str,
    payload: MigrateTermTypeRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
) -> MigrateTermTypeResponse:
    try:
        await require_active_tenant(review_conn, tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="租户不存在或未启用")
    terms_migrated = await migrate_term_type(
        review_conn, tenant_id, old_type=payload.old_type, new_type=payload.new_type
    )
    graph_nodes_migrated = await graph_client.migrate_term_type_nodes(
        tenant_id=tenant_id, old_type=payload.old_type, new_type=payload.new_type
    )
    return MigrateTermTypeResponse(
        terms_migrated=terms_migrated, graph_nodes_migrated=graph_nodes_migrated
    )
```

import 区把 `migrate_term_type` 加进 `from app.graphrag.terms_store import (...)` 那组导入（如果这个文件目前没有从 `terms_store` 导入任何东西，新增一行 `from app.graphrag.terms_store import migrate_term_type`）。

- [ ] **Step 4: 补测试**

`tests/graphrag/test_neo4j_client.py`：照抄 `test_migrate_relation_type_edges_sends_expected_query` 的模式（`FakeSession(rows=[{"migrated_count": 3}])`），新增：
- `test_migrate_term_type_nodes_sends_expected_query`：断言返回值、`session.last_parameters == {"tenant_id": "t1", "old_type": "旧类型", "new_type": "新类型"}`、查询字符串里包含 `"MATCH (t:Term {tenant_id: $tenant_id, type: $old_type})"` 和 `"SET t.type = $new_type"`。
- `test_migrate_term_type_nodes_returns_zero_when_no_matching_nodes`：`FakeSession(rows=[])`，断言返回 0。

（不需要像关系类型那样测格式校验/注入 payload——`migrate_term_type_nodes` 全程参数化传值，没有格式校验这一步。）

`tests/api/test_admin_ontology_routes.py`：
- 新增测试覆盖 `GET .../term-types?status=confirmed` 和默认 `status=draft` 两种情况，确认返回内容符合预期（草稿创建的类型只在 draft 查询里出现）。
- 新增测试覆盖 `POST .../term-types/migrate`：创建草稿类型 → 确认 → 写几条引用该类型的 `terms` 行 → 调用迁移接口 → 断言 `terms_migrated`/`graph_nodes_migrated` 符合预期（`graph_client` 用现有 fixture 里的 fake/mock 客户端，参照文件里 `migrate_relation_type_edges` 那个既有测试的 fixture 用法）。
- 新增测试覆盖迁移接口对未知租户返回 404（照抄本文件其它写接口的既有 404 测试模式）。

- [ ] **Step 5: 跑测试**

```bash
python -m pytest tests/graphrag/test_neo4j_client.py tests/api/test_admin_ontology_routes.py -q
```
Expected: 全部通过。

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/neo4j_client.py app/api/admin_ontology_routes.py tests/graphrag/test_neo4j_client.py tests/api/test_admin_ontology_routes.py
git commit -m "feat(api): add term-type status filtering and cross-store migration endpoint"
```

---

### Task 5: 前端 `TermTypesTab` 接入草稿/已确认

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`

**Interfaces:**
- Consumes：Task 4 的 `GET .../term-types?status=`、`POST .../term-types/migrate`；`OntologySchemaPage` 页面级已有的 `view`/`confirmVersion`/`bumpReadiness`（本计划开始前就已存在，见页面顶部的"前置条件"改造）。

- [ ] **Step 1: `TermTypesTab` 加 props，接入 `view`**

`TermTypesTab` 的 props 类型和函数签名：

```tsx
function TermTypesTab({
  sessionToken,
  tenantId,
  onError,
  view,
  confirmVersion,
  onDataChanged,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
  view: ViewMode
  confirmVersion: number
  onDataChanged: () => void
}) {
```

`refresh` 函数体里的 GET 请求：

```tsx
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`,
        sessionToken,
      )
```

改成：

```tsx
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=${view}`,
        sessionToken,
      )
```

`refresh` 的 `useCallback` 依赖数组加 `view`；下面的 `useEffect(() => { refresh()... }, [refresh])` 改成 `}, [refresh, confirmVersion])`（照抄 `RelationTypesTab`/`ConstraintsTab` 现有的写法）。

- [ ] **Step 2: 新增/编辑/删除操作只在草稿视图可用**

`submit`（新增/编辑表单提交）和 `handleDelete` 成功后各加一行 `onDataChanged()`（照抄 `RelationTypesTab` 的 `submit`/`handleDelete`）。

JSX 里"+ 新增实体类型"按钮、编辑/删除操作列、表单，全部包一层 `view === 'draft' &&`（照抄 `RelationTypesTab` 现有对 `+ 新增关系类型`/编辑/删除/表单的包法）。已确认视图（`view === 'confirmed'`）下表格照常展示（只读），但不出现任何写操作入口。

表格的"操作"列表头和单元格也要包 `{view === 'draft' && (...)}`（照抄 `RelationTypesTab` 表格 `<th>`/`<td>` 那两处 `{view === 'draft' && ...}` 的写法）。

- [ ] **Step 3: 新增"迁移实体类型…"工具**

照抄 `RelationTypesTab` 现有的迁移图谱边整套 UI（`migratingFrom`/`migrateTarget`/`migrating`/`migrateSuccessMessage` 四个 state + 触发按钮 + 表单 + 二次确认弹窗），迁移目标下拉框的选项来自当前 `items`（排除正在迁移的那个）。提交时调用新接口：

```tsx
  const handleMigrate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || migratingFrom === null || migrating) return
    if (
      !window.confirm(
        `这会把租户「${tenantId}」所有 term_type 为「${migratingFrom}」的真实术语和图谱节点批量改成「${migrateTarget}」，不可逆。确定要继续吗？`,
      )
    ) {
      return
    }
    onError(null)
    setMigrating(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/migrate`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_type: migratingFrom, new_type: migrateTarget }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '迁移实体类型失败'))
      }
      const data = (await response.json()) as { terms_migrated: number; graph_nodes_migrated: number }
      setMigrateSuccessMessage(`已迁移 ${data.terms_migrated} 条术语、${data.graph_nodes_migrated} 个图谱节点`)
      setMigratingFrom(null)
      setMigrateTarget('')
    } catch (err) {
      onError(err instanceof Error ? err.message : '迁移实体类型失败')
    } finally {
      setMigrating(false)
    }
  }
```

迁移入口按钮（"迁移实体类型…"）跟"编辑"/"删除"按钮并排放在表格操作列里，只在 `view === 'draft'` 时出现（迁移的是"已确认版本正在使用的旧类型"，但触发入口跟其它草稿操作放在一起，逻辑上是"针对这个草稿类型，把线上还在用旧名字的数据同步过来"）。

- [ ] **Step 4: `OntologySchemaPage` 调用 `TermTypesTab` 传入新 props**

```tsx
      {tab === 'term-types' && (
        <TermTypesTab
          key={tenantId}
          sessionToken={sessionToken}
          tenantId={tenantId}
          onError={setPageError}
          view={view}
          confirmVersion={confirmVersion}
          onDataChanged={bumpReadiness}
        />
      )}
```

`isLifecycleTab` 的定义（页面顶部）从：

```tsx
  const isLifecycleTab = tab === 'relation-types' || tab === 'constraints'
```

改成：

```tsx
  const isLifecycleTab = tab === 'term-types' || tab === 'relation-types' || tab === 'constraints'
```

页面级"确认前置条件"检查（`readiness` 那段 `useEffect`）里，实体类型的 GET 请求：

```tsx
          adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`, sessionToken),
```

改成：

```tsx
          adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=draft`, sessionToken),
```

- [ ] **Step 5: 验证**

```bash
cd frontend && npx tsc --noEmit
```
Expected: 无报错。

手动验收（后端需先跑起来，走完 Task 1-4）：进"实体类型"tab，新增一个类型，确认它只出现在"草稿"视图，不出现在"已确认版本"视图；点"确认 schema"（前提是关系类型/约束草稿也非空）；确认后切到"已确认版本"能看到刚才新增的类型；再回草稿改个名字，确认已确认版本里的名字没变；点"迁移实体类型…"，确认能把改名同步过去。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/admin/OntologySchemaPage.tsx
git commit -m "feat(frontend): give TermTypesTab a draft/confirmed view and a migrate tool"
```

---

### Task 6: 前端剩余下拉框接入 status 参数

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`（`ConstraintsTab` 部分）
- Modify: `frontend/src/admin/TermsPage.tsx`

**Interfaces:**
- Consumes：Task 4 的 `GET .../term-types?status=`。

- [ ] **Step 1: `ConstraintsTab` 的 subject/object 下拉框改查草稿**

`OntologySchemaPage.tsx` 里 `ConstraintsTab` 的 `refresh` 函数，`Promise.all` 里拉实体类型列表那一行：

```tsx
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`, sessionToken),
```

改成：

```tsx
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=draft`, sessionToken),
```

（旁边"关系类型（草稿）"下拉框已经是这么做的，这里补齐保持一致——约束表单里 subject/object 类型选择器现在也只能选草稿中的实体类型。）

- [ ] **Step 2: `TermsPage.tsx` 下拉框改查已确认**

`frontend/src/admin/TermsPage.tsx` 第 72 行左右，拉实体类型枚举给术语表单下拉框用的请求：

```tsx
    adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`, sessionToken)
```

改成：

```tsx
    adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken)
```

- [ ] **Step 3: 验证**

```bash
cd frontend && npx tsc --noEmit
```
Expected: 无报错。

手动验收：在"约束"tab 新增约束时，subject/object 下拉框只列出实体类型的草稿；在"术语库管理"新增/编辑真实术语时，类型下拉框只列出已确认的实体类型（草稿中的不出现，直到点了"确认 schema"）。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/admin/OntologySchemaPage.tsx frontend/src/admin/TermsPage.tsx
git commit -m "fix(frontend): scope term-type dropdowns to draft (constraints) or confirmed (real terms)"
```

## 执行顺序说明

Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6。Task 1 是地基（表结构+核心 CRUD），Task 2/3 都依赖它的 `list_term_types(status=...)` 签名；Task 4 依赖 Task 3 的 `migrate_term_type`；Task 5/6 是前端，依赖 Task 4 的路由改动。全程严格按顺序单线程派发，不并行。
