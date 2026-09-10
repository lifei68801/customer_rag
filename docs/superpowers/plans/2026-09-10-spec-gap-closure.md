# Spec 缺口收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `2026-09-08-interaction-redesign-design.md` 对照代码之后剩下的四条缺口补掉：组织管理页、失效引导问题上看板、看板卡片的表格行数、属性冲突精确到行。

**Architecture:** 四条互不依赖，各自一个任务。全部沿用既有的聚合点：看板数字继续只从 `collect_tenant_stats` 出（口径不分叉）；冲突的行号跟着已有的 `extra_property_sources` 走一条平行的列；组织管理页复用已有的 `admin_org_routes` 与 `PUT /tenants/{id}/organization`，**不新增后端端点**。

**Tech Stack:** FastAPI · aiosqlite · React 18 + TypeScript · vitest

**Spec:** `docs/superpowers/specs/2026-09-08-interaction-redesign-design.md`

## Global Constraints

同该 spec §8 十二条，逐字适用。特别重申：

- **看板数字只能从 `collect_tenant_stats` 出。** 新字段加在那里，路由和卡片照抄；另写一份 SQL 就是口径分叉。
- **`extra_property_sources` 的格式一个字符都不动**——它已经在库里了。行号走一条新的平行列。
- **新列的迁移必须排在重建型迁移之后**（阶段四 I-6 的教训：排在前面会被表重建丢掉）。
- **不许在注释里写未经验证的因果**（本项目已连续七次栽在这里）。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `frontend/src/admin/OrganizationsPage.tsx`（新） | 组织列表 + 新建 + 把租户挂到组织下 |
| `frontend/src/admin/organizations.test.tsx`（新） | |
| `frontend/src/adminRoutes.ts` / `App.tsx` / `AccountMenu.tsx`（改） | 路由、挂载、账号菜单入口 |
| `app/graphrag/tenant_stats.py`（改） | 加 `stale_question_count` 与 `sheet_row_count` |
| `app/api/admin_dashboard_routes.py`（改） | 两个字段透出 |
| `frontend/src/admin/DomainCard.tsx`（改） | 「表格行数」格 + 「N 条引导问题失效」入口 |
| `app/graphrag/terms_store.py`（改） | `extra_property_source_rows` 列 + `incoming_row_number` 参数 |
| `app/graphrag/attribute_conflicts.py`（改） | `kept_row_number` / `incoming_row_number` 两列 |
| `app/graphrag/schema_etl.py`（改） | 把 `projected.row_number` 传下去 |
| `app/api/admin_conflicts_routes.py` / `frontend/src/admin/AttributeConflictsPage.tsx`（改） | 透出并显示「第 N 行」 |

---

## Task 1: 组织管理页

**Files:**
- Create: `frontend/src/admin/OrganizationsPage.tsx`
- Create: `frontend/src/admin/organizations.test.tsx`
- Modify: `frontend/src/adminRoutes.ts`（`organizations: '/admin/organizations'`，加进 `NON_TENANT_ROUTE_KEYS` 与 `EXTRA_TITLES`）
- Modify: `frontend/src/App.tsx`（挂路由）
- Modify: `frontend/src/admin/AccountMenu.tsx`（「租户管理」旁加「组织管理」，同样 `isAdmin && showManagementLinks`）

**Interfaces:**
- Consumes（**已存在，一个都不新增**）：
  - `GET /api/admin/organizations` → `{organizations: [{org_id, name, status, tenant_ids}]}`
  - `POST /api/admin/organizations` `{org_id, name}` → 201
  - `GET /api/admin/tenants` → 租户列表（`TenantsPage` 已在用）
  - `PUT /api/admin/tenants/{tenant_id}/organization` `{org_id: string | null}`

- [ ] **Step 1: 写失败测试**

```tsx
it('列出组织和各自名下的租户', async () => {})
it('新建组织后列表里多一个', async () => {
  // 断言请求体 {org_id, name} 且列表刷新。
})
it('把一个租户挂到组织下发的是 PUT /tenants/{id}/organization', async () => {
  // 断言 URL 和 body {org_id}。
})
it('把租户移出组织发的是 org_id: null，不是空串', async () => {
  // 后端把 null 当"移出"，空串会被当成一个叫 "" 的组织。
})
it('同 org_id 建两次时把后端那句话原样显示', async () => {})
it('member 打不开这一页', async () => {
  // 账号菜单里没有入口，直接输 URL 也要被挡（沿用 accounts/tenants 的守法）。
})
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

页面结构：上半「新建组织」表单（org_id + name）；下半每个组织一张卡，列它名下的租户，
每个租户旁一个下拉「所属组织」（含「无」），改了就 PUT。**没挂组织的租户也要列**
（单独一组「未归入组织」），否则用户找不到它们、也就永远挂不上。

- [ ] **Step 5: 变异 + 提交**

```bash
# A：移出组织时发空串       → 预期红「移出组织发的是 null」
# B：PUT 的 URL 拼错租户段   → 预期红「挂到组织下发的是 PUT」
# C：未归入组织的租户不列   → 预期红「列出组织和各自名下的租户」（造一个无组织的租户）
```

```bash
git add frontend/src/admin/OrganizationsPage.tsx frontend/src/admin/organizations.test.tsx frontend/src/adminRoutes.ts frontend/src/App.tsx frontend/src/admin/AccountMenu.tsx
git commit -m "feat(admin): 组织管理页，建组织、把租户挂进去，只有 admin 能进"
```

---

## Task 2: 失效引导问题上看板 + 表格行数

**Files:**
- Modify: `app/graphrag/tenant_stats.py`
- Modify: `app/api/admin_dashboard_routes.py`
- Modify: `frontend/src/admin/DomainCard.tsx`
- Test: `tests/graphrag/test_tenant_stats.py`（追加）、`frontend/src/admin/dashboard.test.tsx`（追加）

**Interfaces:**
- Produces: `TenantStats.stale_question_count: int`、`TenantStats.sheet_row_count: int`；
  `DomainStats` 同名两字段。

**「表格行数」的定义（裁定，写进注释）**：`COUNT(*) FROM terms WHERE tenant_id = ?
AND source = 'etl' AND standard_name != '__deleted__'`——即表格/数据库导入进来、
且没被人工删除的实体行数。它是 `term_count` 的子集。不用 `etl_runs.report_json`
里的 `entities_written` 求和：那是"历次跑批写了多少次"，重跑三次就翻三倍，
而卡片上要的是"现在有多少"。

**「失效引导问题」的口径**：与 `GET /{tenant_id}/persona/stale-questions` 完全同一个
函数 `find_unmatched_questions(get_questions(...), list_terms_merged(...))`——两处
必须一个数。

- [ ] **Step 1: 写失败测试**

```python
async def test_sheet_row_count_only_counts_etl_rows():
    """手工录入和审核创建的实体不算表格行。三种来源各造一条。"""

async def test_sheet_row_count_excludes_manually_deleted_rows():
    """人工删除（__deleted__）的 etl 行不算：看板上的数字要等于用户在
    实体明细页数得出来的。"""

async def test_stale_question_count_matches_the_persona_endpoint():
    """配三条手写问题，删掉其中一条命中的实体，计数是 1。"""
```

```tsx
it('卡片上有「表格行数」', async () => {})
it('有失效的引导问题时卡片上出现一条待办并能跳到数字人页', async () => {})
it('没有失效问题时那条待办不出现', async () => {})
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

- [ ] **Step 5: 变异 + 提交**

```bash
# A：sheet_row_count 数全部 terms            → 预期红 only_counts_etl_rows
# B：不排除 __deleted__                       → 预期红 excludes_manually_deleted
# C：stale_question_count 恒 0               → 预期红 matches_the_persona_endpoint + 前端那条
# D：待办恒显示                               → 预期红「没有失效问题时那条待办不出现」
```

```bash
git add app/graphrag/tenant_stats.py app/api/admin_dashboard_routes.py tests/graphrag/test_tenant_stats.py frontend/src/admin/DomainCard.tsx frontend/src/admin/dashboard.test.tsx
git commit -m "feat(dashboard): 卡片补表格行数，失效的引导问题变成一条待办"
```

---

## Task 3: 属性冲突精确到行

**Files:**
- Modify: `app/graphrag/terms_store.py`（新列 `extra_property_source_rows TEXT NOT NULL DEFAULT '{}'`；
  `upsert_term_with_node_key(..., incoming_row_number: int | None = None)`；
  `_keep_old_values_and_record_conflicts` 多维护一份 `{field: row}`）
- Modify: `app/graphrag/attribute_conflicts.py`（`kept_row_number INTEGER` / `incoming_row_number INTEGER`，
  用 `add_column_if_missing`；`record_conflict` / `list_conflicts` 透传）
- Modify: `app/graphrag/schema_etl.py:212` 附近（`incoming_row_number=projected.row_number`）
- Modify: `app/api/admin_conflicts_routes.py`（response model 两字段）
- Modify: `frontend/src/admin/AttributeConflictsPage.tsx`（`来自 商品表.csv 第 88 行`；没有行号时只写文件名）
- Test: `tests/graphrag/test_attribute_conflicts.py`、`tests/graphrag/test_schema_etl.py`、
  `frontend/src/admin/attributeConflicts.test.tsx`（各追加）

- [ ] **Step 1: 写失败测试**

```python
async def test_a_conflict_records_both_row_numbers():
    """第一次导入第 3 行写了 39，第二次导入第 12 行给了 45：
    kept_row_number == 3，incoming_row_number == 12。"""

async def test_rows_written_before_this_column_existed_have_no_row_number():
    """存量行没有记录，行号是 None，不是 0——0 会被读成"第 0 行"。"""

async def test_the_real_etl_path_passes_the_row_number():
    """走 run_schema_etl，两份 CSV 冲突，冲突表里有行号。
    store 层用例直接传参数，谁也没问过 ETL 真的传了吗（阶段四 C-1）。"""
```

```tsx
it('冲突两边都写出文件名和第几行', async () => {})
it('没有行号的一边只写文件名，不写「第 0 行」', async () => {})
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

迁移顺序：`extra_property_source_rows` 的 `add_column_if_missing` 放在 `terms_store.py`
现有 `extra_property_sources` 那一条**紧后面**（它已经在重建型迁移之后）。

- [ ] **Step 5: 变异 + 提交**

```bash
# A：ETL 不传 incoming_row_number         → 预期红 the_real_etl_path_passes_the_row_number
# B：kept_row_number 存成 incoming 的      → 预期红 records_both_row_numbers
# C：存量行行号写 0                        → 预期红 have_no_row_number
# D：前端把 null 显示成「第 0 行」          → 预期红 前端第二条
```

```bash
git add app/graphrag/terms_store.py app/graphrag/attribute_conflicts.py app/graphrag/schema_etl.py app/api/admin_conflicts_routes.py tests/graphrag/test_attribute_conflicts.py tests/graphrag/test_schema_etl.py frontend/src/admin/AttributeConflictsPage.tsx frontend/src/admin/attributeConflicts.test.tsx
git commit -m "feat(review): 属性冲突记下两边各来自第几行"
```

---

## Task 4: 更新 spec 与收尾

- [ ] **Step 1: 把「实体消歧与待确认匹配合并」的裁定写回 spec D4**（一句话 + 指向阶段四计划）
- [ ] **Step 2: 后端全量 + 前端全量 + tsc**
- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/specs/2026-09-08-interaction-redesign-design.md
git commit -m "docs(spec): 实体消歧并入待确认匹配页，spec 跟上阶段四的裁定"
```

---

## Self-Review

**Spec coverage**：四条缺口各对应一个任务；D4 的文档不一致在 Task 4。

**Type consistency**：`stale_question_count` / `sheet_row_count` 在 `TenantStats`、`DomainStats`、`DomainCard` 三处同名；`kept_row_number` / `incoming_row_number` 在 store、路由、前端三处同名，均为 `int | None`。

**已知的执行前不确定项**：`admin_conflicts_routes.py` 的 response model 字段名要读文件确认；`test_tenant_stats.py` 是否存在——不存在就新建。
