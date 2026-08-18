# 实体类型接入草稿/已确认生命周期 — 设计文档

## 背景

本体 Schema 管理页面（`/admin/ontology`）目前有三个分类：实体类型、关系类型、约束。其中关系类型（`tenant_relation_types`）和约束（`term_type_relation_allowlist`）已经有完整的草稿/已确认两态生命周期（`app/graphrag/ontology_lifecycle.py::checkout_draft`/`confirm_ontology`），实体类型（`ontology_term_types`）完全没有——新增/修改/删除一个实体类型是立刻生效的，没有草稿状态。

前一轮 UI 改造给"确认 schema"按钮加了前置条件检查（要求实体类型、关系类型草稿、约束草稿三者都至少有一条才能点击），但那只是**存在性检查**，没有改变实体类型本身"立刻生效、没有草稿"的事实。本文档设计把实体类型也接入真正的草稿/已确认两态，跟关系类型/约束完全对齐。

## 决策摘要（grill-me 已确认）

1. **实体类型新增真正的草稿/已确认两态**，机制与关系类型（`tenant_relation_types`）完全一致：表主键从 `(tenant_id, value)` 变成 `(tenant_id, value, status)`，新增走草稿，`checkout_draft`/`confirm_ontology` 统一管理生命周期。
2. **真实术语（`terms` 表）新增/编辑时，`term_type` 只能选已确认的实体类型**，不能选草稿中的——草稿是工作区，确认前不影响已有业务数据，跟关系类型/约束现有的设计理念一致。
3. **约束（`term_type_relation_allowlist`）里 subject/object 引用的实体类型，校验对象是草稿中的实体类型**（不是已确认的）——约束条目必须与实体类型在同一草稿编辑会话里构建，这是仿照 `ontology_constraints.py::_validate_references` 里关系类型校验已经落地验证过的现有设计（校验 draft 关系类型而非 confirmed）。
4. **改名/删除一个已确认的实体类型，只改草稿定义，不会自动级联更新已确认版本正在使用的真实术语**——新增一个"迁移实体类型"工具（同时改 SQLite `terms` 表和 Neo4j `:Term` 节点属性），仿照关系类型现有的"迁移图谱边"工具。
5. **删除草稿中的实体类型时，如果已确认版本里还有真实术语在用，拦住删除**（`CategoryInUseError`，跟现状一致，只是把检查范围收窄到"仍会影响已确认数据"的场景）。

## 现状代码基线（写 plan 时的精确参照）

- `app/graphrag/ontology_categories.py` — 实体类型的表结构+CRUD，目前无 status 概念。
- `app/graphrag/ontology_relations.py` — 关系类型的表结构+CRUD，**是本次改造要照抄的目标形态**（`status TEXT NOT NULL` + 复合主键 `(tenant_id, relation_type, status)`；`create_relation_type` 固定 `status='draft'`；`update_relation_type`/`delete_relation_type` 只操作 `status='draft'` 的行）。
- `app/graphrag/ontology_lifecycle.py` — `_TABLES_WITH_TENANT_LIFECYCLE = ("tenant_relation_types", "term_type_relation_allowlist")`，`checkout_draft`/`confirm_ontology`/`is_ontology_confirmed` 目前只管这两张表。
- `app/graphrag/ontology_constraints.py::_validate_references` — 校验约束的关系类型时用 `status="draft"`（第 71 行），校验实体类型时目前无 status 区分（第 65 行）。
- `app/graphrag/neo4j_client.py::migrate_relation_type_edges`（第 489 行起）— 关系类型改名的图谱迁移工具，Neo4j 关系类型是边标签，改名要"建新边+复制属性+删旧边"。**实体类型不一样**：`term_type` 是 `:Term` 节点的属性（`t.type`，见 `neo4j_client.py` 第 395 行 `"type": term.term_type`），改名只需要对匹配节点 `SET t.type = $new_type`，比关系类型迁移简单得多，不需要建新删旧。
- 全仓库调用 `list_term_types()` 的 6 个地方（`grep -rn "list_term_types(" app/`），本次改造后各自需要的 status 参数：

| 调用点 | 用途 | status |
|---|---|---|
| `app/api/admin_ontology_routes.py:78`（`list_term_type_categories` GET 路由） | 管理页面列表展示 | 新增 query 参数 `status: str = "draft"`（跟 `list_tenant_relation_types` 的默认值一致） |
| `app/api/deps.py:382`（`get_term_type_schema`） | 结构化过滤查询工具校验，生产运行时路径 | `"confirmed"`（紧邻的 `get_confirmed_relation_types` 已经是这么做的） |
| `app/graphrag/ontology_constraints.py:65`（`_validate_references`） | 校验约束的 subject/object 类型是否存在 | `"draft"`（决策 3） |
| `app/graphrag/schema_etl.py:89`（`_write_entity_mapping`） | ETL 写入校验，报错文案已经写着"不在已确认 schema 里" | `"confirmed"` |
| `app/graphrag/terms_store.py:176`（`_bridge_seed_categories_from_existing_terms`） | 一次性从历史 `terms` 数据反向种分类枚举（仅在枚举表全空时触发） | `"confirmed"`（种进去的历史数据本来就是"当前生效"的） |
| `app/graphrag/terms_store.py:312`（`_validate_categories`） | 真实术语新增/编辑时校验 term_type | `"confirmed"`（决策 2） |

- 全仓库前端 fetch `.../term-types` 的 3 个地方，各自需要的 status：

| 位置 | 用途 | status |
|---|---|---|
| `frontend/src/admin/TermsPage.tsx:72` | 真实术语表单的类型下拉框 | `confirmed` |
| `frontend/src/admin/OntologySchemaPage.tsx`（页面级"确认前置条件"检查） | 判断实体类型草稿是否非空 | `draft` |
| `frontend/src/admin/OntologySchemaPage.tsx`（`ConstraintsTab` 的 subject/object 下拉框） | 约束表单的类型下拉框 | `draft`（决策 3，跟旁边"关系类型（草稿）"下拉框保持一致） |
| `frontend/src/admin/OntologySchemaPage.tsx`（`TermTypesTab` 自己的列表） | 跟 `RelationTypesTab` 一样按 `view` prop 切换 | `${view}`（draft 或 confirmed） |

## 后端改造范围

### 1. `app/graphrag/ontology_categories.py`

- 表结构加 `status` 列，主键变成 `(tenant_id, value, status)`：

```sql
CREATE TABLE IF NOT EXISTS ontology_term_types (
    tenant_id         TEXT NOT NULL,
    value             TEXT NOT NULL,
    extra_fields      TEXT NOT NULL DEFAULT '[]',
    node_key_template TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL,
    PRIMARY KEY (tenant_id, value, status)
);
```

- 新增迁移函数 `_migrate_term_types_add_status_if_needed`（跟现有 `_migrate_term_types_table_if_needed` 同构：查 `PRAGMA table_info` 有没有 `status` 列，没有就整表重建，存量行全部落 `status='confirmed'`——它们迁移前本来就是"直接生效"的，语义上对应新模型里的已确认，不是草稿）。在 `ensure_categories_schema` 里紧跟在 `_migrate_term_types_table_if_needed` 之后调用。

- `list_term_types` 加必填的 `*, status: str` 参数（不给默认值，强制每个调用点显式声明，避免遗漏——参照 `list_relation_types` 的签名）。

- `create_term_type` 的 INSERT 加 `status` 列，固定写 `'draft'`；`CategoryNameConflictError` 文案改成"已经是该租户草稿里的分类"（跟 `create_relation_type` 的报错文案对齐）。

- `update_term_type`：查找/更新都加 `AND status = 'draft'`；找不到草稿行时报 `CategoryNotFoundError`；级联更新的范围从"`terms` 表 + `term_type_relation_allowlist`"收窄成**只有** `term_type_relation_allowlist`（且只更新 `status = 'draft'` 的行）——**不再级联更新 `terms` 表**（决策 4，这是唯一一处删掉现有级联逻辑的地方）。

- `delete_term_type`：`terms` 表使用量检查保持不变（真实术语只引用已确认类型，这个检查天然对应"已确认版本是否在用"）；`term_type_relation_allowlist` 使用量检查加 `AND status = 'draft'`（决策 5：只挡住会影响"当前草稿自洽性"和"已确认业务数据"的情况，不挡跟本次删除无关的已确认约束）；DELETE 语句加 `AND status = 'draft'`（只删草稿行）。

- 新增 `migrate_term_type`（SQLite 侧，批量把 `terms.term_type` 从旧值改成新值，供后面 Task 里的迁移工具用）：

```python
async def migrate_term_type(
    conn: aiosqlite.Connection, tenant_id: str, *, old_type: str, new_type: str
) -> int:
    cursor = await conn.execute(
        "UPDATE terms SET term_type = ? WHERE tenant_id = ? AND term_type = ?",
        (new_type, tenant_id, old_type),
    )
    await conn.commit()
    return cursor.rowcount
```

（这个函数放在 `terms_store.py` 更合适，因为它操作的是 `terms` 表，不是 `ontology_term_types`——跟 `ontology_categories.py` 里其它函数的职责边界一致，写 plan 时把它放进 `app/graphrag/terms_store.py`。）

### 2. `app/graphrag/ontology_lifecycle.py`

- `_TABLES_WITH_TENANT_LIFECYCLE` 加入 `"ontology_term_types"`（变成三元组）。
- `checkout_draft` 加第三段逻辑：没有草稿但有已确认版本 → 复制已确认到草稿；两者都没有 → **不播种任何默认值**（跟关系类型不一样，实体类型没有"通用默认类型"这种东西，完全依赖业务定义）。
- `confirm_ontology` 的 `has_draft_in_any_table` 检查加上 `ontology_term_types`。
- `is_ontology_confirmed` **保持不变**，只检查 `tenant_relation_types` 有已确认行——"实体类型/关系类型/约束三者都要有草稿才能点确认"目前只是前端 UI 层面的把关（`OntologySchemaPage.tsx` 的 `readiness` 检查），后端 `confirm_ontology()` 本身并不强制这个前提，直接调 API 依然可以在实体类型草稿为空的情况下确认关系类型。如果这里也要求 `ontology_term_types` 有已确认行，会让 `is_ontology_confirmed` 出现一个后端自己都不保证成立的假设，还会破坏 `tests/graphrag/test_ontology_lifecycle.py::test_confirm_ontology_promotes_draft_to_confirmed` 等现有测试（这些测试场景就是只建关系类型、不建实体类型，直接确认）。把"三者都非空才能确认"做成后端硬约束不在本次范围内，需要的话应另起一次 grill-me 单独决策。

### 3. `app/graphrag/ontology_constraints.py`

- `_validate_references` 里校验实体类型存在性的 `list_term_types(conn, tenant_id)` 调用改成 `list_term_types(conn, tenant_id, status="draft")`。

### 4. `app/graphrag/terms_store.py`

- `_validate_categories` 里的 `list_term_types(conn, tenant_id)` 改成 `status="confirmed"`。
- `_bridge_seed_categories_from_existing_terms`：判空检查的 `list_term_types(conn, tenant_id)` 改成 `status="confirmed"`；下面的 `INSERT OR IGNORE INTO ontology_term_types (...)` 语句加 `status` 列，写死 `'confirmed'`（这段是从历史 `terms` 数据反向种分类，种出来的数据代表"已经在用"，不是草稿）。
- 新增 `migrate_term_type`（见上方第 1 节的函数体，最终落在这个文件）。

### 5. `app/graphrag/schema_etl.py`

- `_write_entity_mapping` 的 `list_term_types(conn, tenant_id)` 改成 `status="confirmed"`。

### 6. `app/api/deps.py`

- `get_term_type_schema` 的 `list_term_types(review_conn, tenant_id)` 改成 `status="confirmed"`。

### 7. `app/graphrag/neo4j_client.py`

- 新增 `migrate_term_type_nodes`：

```python
async def migrate_term_type_nodes(
    self, *, tenant_id: str, old_type: str, new_type: str
) -> int:
    """把某个租户所有旧 term_type 的 :Term 节点属性批量改成新值，返回迁移的
    节点数。term_type 是节点属性（t.type），不是边标签——不像
    migrate_relation_type_edges 那样要"建新边、复制属性、删旧边"，这里原地
    SET 一下就行。
    """
    query = (
        "MATCH (t:Term {tenant_id: $tenant_id, type: $old_type}) "
        "SET t.type = $new_type "
        "RETURN count(t) AS migrated_count"
    )
    async with self._driver.session() as session:
        result = await session.run(query, tenant_id=tenant_id, old_type=old_type, new_type=new_type)
        record = await result.single()
        return record["migrated_count"] if record else 0
```

### 8. `app/api/admin_ontology_routes.py`

- `list_term_type_categories`（GET）加 `status: str = "draft"` query 参数，透传给 `list_term_types`。
- 新增迁移路由 `POST /{tenant_id}/term-types/migrate`，body `{old_type, new_type}`，仿照 `migrate_tenant_relation_type` 的结构：先 `require_active_tenant`，再依次调用 `terms_store.migrate_term_type`（SQLite）和 `graph_client.migrate_term_type_nodes`（Neo4j），返回 `{"terms_migrated": N, "graph_nodes_migrated": M}`（两个数分开报告，不合并成一个数——两边可能不一致，比如某条术语当初图谱同步失败过，分开报告更诚实、更方便排查）。
- `create_term_type_category`/`update_term_type_category`/`delete_term_type_category` 的错误映射不用改（`CategoryNotFoundError`→404、`CategoryNameConflictError`→400、`CategoryInUseError`→409 都已经在，只是背后的判定范围变了）。

## 前端改造范围

### `frontend/src/admin/OntologySchemaPage.tsx`

- `isLifecycleTab` 从 `tab === 'relation-types' || tab === 'constraints'` 扩成三个 tab 都算（加 `tab === 'term-types'`）——`产品线` tab 继续是唯一例外（全局配置，无草稿概念）。
- 页面级"确认前置条件"检查（`readiness` state 那段 `useEffect`）里，`term-types` 的 GET 调用加 `?status=draft`。
- `TermTypesTab` 整个改造成跟 `RelationTypesTab` 同构：
  - 加 `view: ViewMode`、`confirmVersion: number`、`onDataChanged: () => void` 三个 prop（跟 `RelationTypesTab`/`ConstraintsTab` 现在的 props 完全对齐）。
  - `refresh()` 的 GET 请求从 `/term-types` 改成 `/term-types?status=${view}`；`useEffect` 依赖数组加 `confirmVersion`。
  - 表单（新增/编辑）、删除按钮只在 `view === 'draft'` 时可用/可见（照抄 `RelationTypesTab` 现有的 `{view === 'draft' && (...)}` 模式）。
  - `submit`/`handleDelete` 成功后调用 `onDataChanged()`（照抄 `RelationTypesTab`）。
  - 新增一个"迁移实体类型…"入口（照抄 `RelationTypesTab` 现有的"迁移图谱边…"按钮 + 表单：选中要迁移的旧类型、下拉选新类型、二次确认弹窗、提交后展示 `已迁移 N 条术语、M 个图谱节点`）。
- `ConstraintsTab` 的 subject/object 下拉框数据源，`/term-types` 请求加 `?status=draft`（跟旁边"关系类型（草稿）"下拉框的既有写法对齐）。
- `OntologySchemaPage` 调用 `<TermTypesTab>` 的地方传入新增的三个 prop（`view`、`confirmVersion`、`onDataChanged={bumpReadiness}`，跟 `RelationTypesTab`/`ConstraintsTab` 现在的调用方式一致）。

### `frontend/src/admin/TermsPage.tsx`

- 第 72 行拉实体类型下拉选项的请求加 `?status=confirmed`。

## 迁移/兼容性

- 现有生产数据（`ontology_term_types` 表里已有的行）迁移后全部标记 `status='confirmed'`，不影响任何已有的术语引用关系——这些类型迁移前后都被真实术语正常引用，迁移只是给它们的状态打上准确的标签。
- 现有前端 `TermTypesTab` 的新增/编辑/删除操作迁移上线后，行为从"立刻生效"变成"进草稿"——这是本次改造的核心变化，需要在 UI 上让用户能明确感知（`view` 分段控件 + 状态徽章已经有，不需要额外提示）。

## 不在本次范围内

- 不改 `extra_fields`/`node_key_template` 本身的语义，只是让整条记录（含这两个字段）跟着状态走。
- 不给"迁移实体类型"工具做分批处理（YAGNI，跟 `migrate_relation_type_edges` 现有的单条 Cypher 处理全部的做法一致，等真出现性能问题再说）。
- 不改 `产品线`（`ontology_product_lines`）——继续是全局配置，无草稿概念，本次决策 1-5 都不涉及它。
- "迁移实体类型"不更新此前 ETL 运行已经算出来的 `node_key`，也不更新 `stable_code_registry` 的 scope——两者都把实体类型字面拼进去当前缀（见 `app/graphrag/schema_etl_row_processing.py` 的 `compute_node_key`、`allocate_stable_code`）。这意味着：对已经跑过 ETL 的租户改名并迁移某个实体类型后，再次运行 ETL 会给同一批源数据算出新的 `node_key`（前缀变了），Cypher 侧 MERGE 不到旧节点，等于新建一份重复的节点，而不是更新已有节点。这是本次改造已知且接受的局限，不在这里修——ETL 模式租户应避免给已经跑过 ETL 的实体类型改名，或者接受改名后的 ETL 重跑会产生并行的节点集合；修复 `node_key`/`stable_code_registry` 与实体类型的耦合是另一个更大的设计问题，需要单独立项。
