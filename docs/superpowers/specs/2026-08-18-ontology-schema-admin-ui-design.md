# 本体 Schema 管理后台界面设计

**状态**：设计定稿，待写执行计划。
**上游依赖**：`app/api/admin_ontology_routes.py`（本体 schema 的全部后端接口，本设计只加前端界面，不改动后端）。

## 0. 问题陈述

`app/graphrag/ontology_categories.py`/`ontology_relations.py`/`ontology_constraints.py`/`ontology_lifecycle.py` 四个模块 + `admin_ontology_routes.py` 的完整 REST 接口早已存在，但没有任何管理后台页面能操作它们——业务方要定义实体类型、关系类型、约束、确认 schema，只能直接调 API。这与已有的文档/术语/ETL 三个管理页面不一致，也是 `SchemaEtlPage.tsx` 页面加载时"schema 未确认则禁用入口"这一提示的死胡同——用户看到提示，却无处可去完成确认。

## 1. 后端接口盘点（既有，不改动）

`app/api/admin_ontology_routes.py`，路由前缀 `/api/admin/ontology`：

| 实体 | 是否分租户 | 是否有 draft/confirm | 接口 |
|---|---|---|---|
| 实体类型（term-types） | 是 | **否**——即写即生效 | `GET/POST /{tenant_id}/term-types`，`PUT/DELETE /{tenant_id}/term-types/{value}` |
| 产品线（product-lines） | **否，全局** | 否 | `GET/POST /product-lines`，`PUT/DELETE /product-lines/{value}` |
| 关系类型（relation-types） | 是 | **是** | `GET/POST /{tenant_id}/relation-types`（`GET` 带 `?status=draft\|confirmed`），`PUT/DELETE /{tenant_id}/relation-types/{relation_type}`，`POST /{tenant_id}/relation-types/migrate`（图谱边批量迁移，独立于改名） |
| 约束（constraints，即 term_type_relation_allowlist） | 是 | **是**（与 relation-types 共用同一次 confirm） | `GET/POST/DELETE /{tenant_id}/constraints`（`GET` 带 `?status=`） |
| 生命周期 | 是 | — | `POST /{tenant_id}/checkout`，`POST /{tenant_id}/confirm`，`GET /{tenant_id}/status` |

关键既有约束（读代码确认，不是猜测）：
- `is_ontology_confirmed` 只看 `tenant_relation_types` 有没有 `confirmed` 行——**relation-types 是否确认，就是整个 schema 是否确认的唯一判据**，term-types/product-lines 不参与这个判断。
- `add_allowed_combination`（约束）校验 `relation_type` 时查的是**该租户 draft 状态**的关系类型列表（`ontology_constraints.py` 第59-64行注释明确解释了这是"经过落地验证的正确设计，不是待定问题"），`term_type` 则查全量（term-types 无 draft 概念）。约束前端下拉框必须遵循同样的数据源，否则会出现"选了一个刚建的关系类型，提交却报未知关系类型"的假故障。
- `node_key_template` 字段目前**只声明和存储，代码里没有任何地方读取它来实际计算 node_key**（ETL 引擎走的是 YAML 配置里独立的 `node_key_parts`）——按 YAGNI，界面把它当纯文本框，不做格式校验/占位符联动。

## 2. 页面结构：合一页面 + 四个 tab

新增导航项"本体 Schema 管理"（`frontend/src/admin/OntologySchemaPage.tsx`，挂载到 `/admin/ontology`），页面顶部固定展示当前租户（复用已有的 `useAdminTenant`/`TenantSwitcher`）+ 确认状态（`GET /{tenant_id}/status`）。页面内部四个 tab：**实体类型｜关系类型｜约束｜产品线**。

产品线 tab 是全局的、不受当前租户切换影响——tab 内容顶部要有一句提示文案（"产品线是全局配置，不属于当前租户"），避免用户以为切租户会看到不同的产品线列表。

## 3. checkout 语义：对用户透明

`checkout_draft` 幂等（已有草稿不覆盖，无草稿则从已确认版本复制或播种默认值）。进入"关系类型"或"约束" tab 时页面自动调用一次 `POST /{tenant_id}/checkout`，用户感知不到这个步骤，直接就能编辑草稿——与 `SchemaEtlPage.tsx`"预检查+自动处理"的既有风格一致。

## 4. 草稿/确认视图切换

关系类型、约束两个 tab 默认展示草稿（`GET ...?status=draft`，可编辑：增删改）。tab 内顶部加一个"查看已确认版本"开关，切换后调 `?status=confirmed`，该视图**只读**（不显示编辑/删除按钮），用于对照当前线上生效的是什么样子。

## 5. 确认（confirm）操作

一个"确认"按钮（放在关系类型 tab，因为 `is_ontology_confirmed` 判据就是这张表），点击后走浏览器原生 `window.confirm`：

```
确认后，当前草稿将成为新的已确认版本，旧的已确认版本会被换掉、
无法恢复。确认要确认租户「{tenantId}」吗？
```

确认后调 `POST /{tenant_id}/confirm`，成功后刷新页面顶部的确认状态、以及关系类型/约束两个 tab 的草稿列表（`confirm_ontology` 会把 draft 提升为 confirmed 并清空原 confirmed，草稿视图理论上不变，但确认状态徽章要立即反映）。**不做** confirm 前的 diff 预览——用户在草稿 tab 里已经能看到完整列表，重复做一份 diff 视图是过度设计。

## 6. 实体类型 tab

列表：`value`（类型名）/ `node_key_template` / 属性字段数。新增/编辑走同一个表单：

```
类型名: [___________]
node_key_template: [___________]（纯文本，不校验格式——见第1节说明）

属性字段:
  字段名              类型
  [numeric_value___] [number   ▾] [删除]
  [dims____________] [number[] ▾] [删除]
  [+ 添加字段]

改名会立即级联更新所有引用该类型的术语记录，没有草稿缓冲。

[保存]
```

类型下拉框固定四个选项：`string`/`number`/`integer`/`number[]`（与 `ontology_categories.py::_VALID_EXTRA_FIELD_VALUE_TYPES` 一致）。删除操作若命中 `CategoryInUseError`（已有术语引用该类型），把后端 400 的 detail 文本直接展示为错误提示，不做额外的"哪些术语在用"查询——那需要新的后端接口，超出本次范围。

## 7. 关系类型 tab

列表：`relation_type` / `example_phrase` / `description` / `allow_chain_query`。新增/编辑表单对应这四个字段（`relation_type` 走 `ontology_relations.py` 的命名格式校验，格式错误时后端 400，直接展示 detail）。

每一行额外有一个"迁移图谱边…"次要操作（与改名的 `PUT` 分开、独立的按钮），点击弹出一个小表单选新类型名，提交前二次确认（说明这会遍历该租户 Neo4j 里所有该类型的边、批量改类型，不可逆），确认后调 `POST /{tenant_id}/relation-types/migrate`，成功后展示"已迁移 N 条边"。改名按钮旁加提示文案："改名只影响草稿定义，已确认图谱里的历史边不会自动变，需要用「迁移图谱边」处理。"

## 8. 约束 tab

新增一行约束：三个下拉框（`subject_term_type` / `relation_type` / `object_term_type`）+ 提交按钮。下拉框数据源：
- `subject_term_type`/`object_term_type`：该租户全部实体类型（`GET /{tenant_id}/term-types`，无 draft 概念）。
- `relation_type`：该租户**草稿**关系类型（`GET /{tenant_id}/relation-types?status=draft`）——与后端 `_validate_references` 校验的数据源保持一致，避免选中一个刚建好但还没进草稿视图缓存的类型时报错。

列表展示当前草稿约束，每行一个删除按钮（`DELETE`，请求体带三个字段原样传回）。

## 9. 产品线 tab

最简单的一个 tab：列表 + 新增（单个 `value` 文本框）+ 每行删除。不分租户，顶部提示见第2节。删除同样直接展示后端 400 detail（`CategoryInUseError`）。

## 10. 范围外事项

- 不做 confirm 前的 draft vs confirmed diff 视图（第5节）。
- 不做"这个实体类型/产品线被哪些术语引用"的详细查询（第6节、第9节）。
- 不做 `node_key_template` 的格式校验/占位符联动（第1节，字段当前未被消费）。
- 不做批量导入/导出 schema 定义（如果未来有需求，是独立的功能，不在本次范围）。
