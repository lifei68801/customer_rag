# 管理后台 UI/UX 审查修复 — 设计文档

## 背景

对前端聊天前台和 5 个后台管理页面（文档管理/知识图谱审核/术语库管理/ETL 跑批/本体 Schema 管理）做了一轮系统性 UI/UX 审查，发现的问题分四类，本文档覆盖全部四类的设计决策。

## 范围

### A. 低风险速修（纯前端，无设计分歧）

1. **`status.error` 对比度不达标**：`tailwind.config.ts` 里 `status.error = #F97264` 直接当文字颜色用（`text-status-error`），在白色/`paper` 背景上实测对比度约 2.75:1，低于 WCAG AA 正文 4.5:1 的门槛。影响 `OntologySchemaPage.tsx` 4 个 tab 的全部"删除"链接（6 处）+ `GraphReviewsPage.tsx` 一处提示文字。
   - 处理：把 `tailwind.config.ts` 里 `status.error` 改成 `#DC2626`（Tailwind red-600，对白色背景对比度约 4.83:1，通过 AA）。这是唯一色值定义，改一处，全部引用自动生效。
   - 顺带统一：`ChatSidebar.tsx` 里两处硬编码的 `text-red-700` 改成 `text-status-error`，让全站错误色只有一个来源。

2. **侧边栏导航顺序与实际工作流不匹配**：`AdminLayout.tsx` 当前顺序是 文档管理 → 知识图谱审核 → 术语库管理 → ETL 跑批 → 本体 Schema 管理。但 `SchemaEtlPage.tsx` 本身要求 schema 必须先确认（`confirmed !== true` 时锁死上传入口），本体 Schema 是所有后续操作的前提。
   - 处理：重排为 本体 Schema 管理 → 文档管理 → 知识图谱审核 → ETL 跑批 → 术语库管理（先立 schema 基础，再是最常用的文档上传，紧接着审核文档抽取出的候选关系，ETL 批量导入相对低频排后面，术语库作为最终产出物放最后浏览/维护）。

3. **产品命名不统一**：导航栏/Footer 用"企业数字员工"，`Hero.tsx` 标题却是"随时待命的Know Know"，`index.html` 静态 `<title>` 又是"客服智能问答 Demo"（会在 JS 接管前短暂闪现）。
   - 处理：以"企业数字员工"为准。`index.html` 的 `<title>` 改成"企业数字员工"（和 `ChatPage.tsx` 运行时设置的一致，消除首帧闪烁）。`Hero.tsx` 标题改成"企业数字员工"，原来的"随时待命的Know Know"文案降级为标题下方的副标题/slogan（不整句删除，只降级视觉层级）。

4. **`SchemaEtlPage.tsx` 无效的 `aria-disabled`**：`<form aria-disabled={confirmed !== true}>` 不是有效的可交互 ARIA 用法（`<form>` 不是 widget role），实际禁用完全靠子元素各自的 `disabled` 属性。
   - 处理：直接删掉这个属性，不改变任何实际行为。

### B. 租户注册表（后端新建 + 前端改造，本次范围最大的一项）

**现状**：`tenant_id` 目前是完全自由的字符串——`app/tenancy.py::is_valid_tenant_id()` 只校验字符集（字母/数字/下划线/连字符），任何符合格式的字符串都能直接当 tenant_id 写入任意管理接口，没有任何注册表记录"当前有哪些租户"。前端 `TenantSwitcher.tsx` 因此被写死成只有一个 `"demo"` 选项。

**决策**（已与用户确认）：

1. **新建正式 `tenants` 注册表**，不用"从现有表 UNION distinct 值"的轻量方案。
2. **全面校验**：所有接受 `tenant_id` 的管理后台写入接口都要在写入前校验该 `tenant_id` 存在于注册表且状态为 `active`。范围限定在管理后台的写接口（`admin_document_routes.py` / `admin_graph_review_routes.py` / `admin_ontology_routes.py` / `admin_schema_etl_routes.py` / `admin_terms_routes.py` 里的 POST/PUT/DELETE），**不**涉及聊天运行时路径（`agent_routes.py` / `qa_routes.py` / `session_routes.py` / `voice_routes.py`）——那些是终端用户查询路径，校验行为对现网影响面和风险都不一样，不在本次范围内，留待后续单独评估。
3. **存量数据迁移时自动回填**：新建 `tenants` 表的迁移脚本里，从现有的两个 SQLite 库分别查出所有历史出现过的 distinct `tenant_id`，合并后批量插入注册表（状态设为 `active`，`name` 默认等于 `tenant_id`）。这样上线校验后不会把已有数据的租户拒之门外。
4. **支持停用（软删除），不支持硬删除**：`tenants` 表带 `status` 字段（`active`/`disabled`）。停用后：该租户从 `TenantSwitcher` 下拉框隐藏，且后续写入被拒绝；但历史数据不删除、不级联。本次不提供硬删除接口。
5. **新建租户的入口在 `TenantSwitcher` 里内联**：下拉框旁/下方加一个"+ 新建租户"，点开是一个小的行内表单（tenant_id + 显示名），提交后自动切到新租户。不新增独立的"租户管理"页面。

#### 技术细节

**新表**（建在 `graph_review_db_path` 对应的库里，即 `get_review_conn` 打开的那个 aiosqlite 连接——本体/术语/etl_runs 都已经在这个库，租户注册表跟着放一起，管理后台的路由本来就都依赖 `review_conn`）：

```sql
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```

新建 `app/graphrag/tenants_store.py`（参照 `etl_runs_store.py` 的既有风格），提供：
- `ensure_tenants_schema(conn)` — 建表 + 迁移回填（幂等，`CREATE TABLE IF NOT EXISTS` + 一次性回填逻辑用 `INSERT OR IGNORE` 保证重复调用安全）。
- `list_tenants(conn, *, include_disabled=False)` — 返回 `[{tenant_id, name, status}]`，默认只返回 active。
- `create_tenant(conn, *, tenant_id, name)` — tenant_id 已存在时抛 `TenantAlreadyExistsError`。
- `tenant_exists_and_active(conn, tenant_id)` — 校验用的轻量查询。
- `set_tenant_status(conn, tenant_id, status)` — 停用/重新启用。

**迁移回填的数据来源**（`ensure_tenants_schema` 内部逻辑）：
- `review_conn` 库内：`SELECT DISTINCT tenant_id FROM terms`、`ontology_term_types`、`etl_runs`、`graph_review_queue`。
- `ingestion_conn` 库内（另一个 SQLite 文件，两个连接不能直接 UNION，`ensure_tenants_schema` 需要拿到 `ingestion_conn` 才能查）：`SELECT DISTINCT tenant_id FROM ingested_documents`。
- 因此 `ensure_tenants_schema` 的调用点要同时拿到 `review_conn` 和 `ingestion_conn` 两个连接——在 `deps.py` 里两个 conn 的初始化时机不同（各自懒加载单例），迁移函数签名设计成 `ensure_tenants_schema(review_conn, ingestion_conn)`，在 `get_review_conn()` 完成自身其它 `ensure_*_schema` 调用后，显式调用 `get_ingestion_conn()` 拿到另一个连接再执行这一步（`deps.py` 里两个 getter 都是幂等的懒加载单例，互相调用不会重复建库）。
- 找不到任何历史 tenant_id 时（全新环境），至少保证 `demo` 存在（种一条 `('demo', 'demo', 'active')`），维持现有默认体验不被破坏。

**校验点**（新增一个共享依赖，仿照 `app/tenancy.py::is_valid_tenant_id` 的角色定位）：
- 在 `app/graphrag/tenants_store.py` 里加 `async def require_active_tenant(conn, tenant_id) -> None`，不存在或非 active 时抛一个新的 `TenantNotFoundError`（继承一个已有的 HTTPException 转换模式，参照本仓库其它 `XxxNotFoundError → 404` 的既有映射方式，比如 `ontology_categories.py::CategoryNotFoundError` 的路由层转换写法）。
- 在 5 个 admin 路由文件的每个写接口（POST/PUT/DELETE）里，格式校验通过后立即调用 `require_active_tenant`。

**新增管理 API**（挂在一个新文件 `app/api/admin_tenant_routes.py`，注册进 `app/main.py` 的路由列表）：
- `GET /api/admin/tenants` → `{tenants: [{tenant_id, name, status}]}`（默认只返回 active，供下拉框用）
- `POST /api/admin/tenants` body `{tenant_id, name}` → 创建，`tenant_id` 需通过 `is_valid_tenant_id` 格式校验 + 不能已存在
- `POST /api/admin/tenants/{tenant_id}/disable` → 停用
- `POST /api/admin/tenants/{tenant_id}/enable` → 重新启用（有停用就要有对应的恢复口子，避免误停用后无法挽回）

**前端改造**（`TenantSwitcher.tsx`）：
- 组件挂载时 `GET /api/admin/tenants`，用返回列表渲染 `<option>`，替换掉硬编码的 `demo`。
- 下拉框下方加"+ 新建租户"按钮，点击展开内联表单（tenant_id 输入框 + 显示名输入框 + 提交/取消），提交成功后调用 `setTenantId(newId)` 自动切换，并把新租户加入本地列表（不用整个重新拉取）。
- 加载失败时的兜底：保留一个仅含当前 `sessionStorage` 里缓存的 `tenantId`（如果有）的选项，避免列表接口挂了导致整个下拉框空白、无法操作。

### C. 术语库 term_type/product_line 改为下拉选择

**现状**：`TermsPage.tsx` 新增/编辑术语时，`term_type`/`product_line` 是自由文本 `<input>`；`OntologySchemaPage.tsx` 把这两个概念做成了严格枚举（分别对应 `GET /api/admin/ontology/{tenant}/term-types` 和 `GET /api/admin/ontology/product-lines`），两处数据模型脱节，容易在术语库里手填出本体里不存在的类型。

**处理**：
- `TermsPage.tsx` 新增 `useEffect`，参照 `GraphReviewsPage.tsx` 已有的"进页面时拉一次术语表用于自动补全"模式，改为拉取 `term-types`（`/api/admin/ontology/{tenantId}/term-types`）和 `product-lines`（`/api/admin/ontology/product-lines`）两个列表。
- 新增术语表单和编辑表单里的 `term_type`/`product_line` 输入框都换成 `<select>`，选项来自上面两个列表；`<option value="">`允许留空（现有数据允许 term_type/product_line 为空，`term.term_type || '（无类型）'` 的展示逻辑不变）。
- 边界情况：如果某条已有术语的 `term_type` 不在当前枚举里（历史脏数据，或者本体那边后来把该类型删了），编辑该术语时下拉框要能显示这个"野值"而不是静默丢弃——用一个不在正式列表里但等于当前值的 `<option>` 兜底渲染（值仍然保留，用户可以选别的覆盖，但不强制先清空）。
- 租户切换时（`tenantId` 变化）要重新拉取这两个列表——`GraphReviewsPage.tsx` 已有的 `useEffect` 依赖 `tenantId` 的写法可以直接照搬。

### D. 文档管理 / 术语库管理补充分页

**现状**：`/api/admin/documents` 和 `/api/admin/{tenant}/terms` 都是一次性返回全量数据，前端也不分页；`GraphReviewsPage.tsx` 已有完整的 `Pager.tsx` + `page`/`page_size`/`total` 模式可以复用。

**关键约束**：`list_terms()`（`terms_store.py`）和 `list_tracked_files()`（`ingestion/tracking.py`）分别被 agent 检索、ingestion 流水线、eval runner、CLI 等多处非分页场景调用（`grep` 确认的调用点包括 `deps.py::get_terms`、`app/eval/runner.py`、`app/graphrag/review_cli.py`、`app/ingestion/main.py`/`incremental_main.py` 等），**不能改变这些函数的默认返回行为**。

**处理**：
- 参照 `review_queue.py::list_pending_reviews` 已有的 `limit: int | None = None, offset: int = 0` 哨兵模式（`limit=None` 时 SQL 里用 `LIMIT -1` 表示不限制），给 `list_terms()` 和 `list_tracked_files()` 加同样签名的可选分页参数，默认值保持不传参时的现有全量行为，不影响任何现有调用点。
- 各新增一个 `count_terms(conn, tenant_id)` / `count_tracked_files(conn, tenant_id)`，参照 `review_queue.py::count_pending_reviews` 的写法（`SELECT COUNT(*) ... WHERE tenant_id = ?`）。
- `admin_terms_routes.py` 的 GET 列表接口和 `admin_document_routes.py` 的 GET 列表接口加 `page`/`page_size` query 参数（默认 `page=1, page_size=20`，与 `GraphReviewsPage` 的 `PAGE_SIZE=20` 保持一致），返回体加 `total` 字段。
- 前端 `TermsPage.tsx`/`DocumentsPage.tsx` 引入 `Pager.tsx`，状态管理模式照抄 `GraphReviewsPage.tsx`（`page`/`total` state + 请求序号防旧响应覆盖新响应 + 增删后自动退页的三个既有 `useEffect`）。
- `DocumentsPage.tsx` 现有的 3s/15s 自动轮询逻辑（`pendingJobs` 驱动的退避间隔）保持不变，只是每次轮询改成带 `page`/`page_size` 参数请求当前页——翻页会重置到目标页后继续轮询那一页。

## 不在本次范围内

- 租户硬删除 / 级联删除关联数据。
- 聊天运行时路径（`agent_routes.py` 等）接入租户注册表校验。
- 完整的设计系统/视觉重做（本次只动色值 token 和局部文案，不改 neo-brutalist 整体风格）。
- `list_pending_jobs`/`list_dead_jobs` 的分页（`admin_document_routes.py` 里已经用 `limit=50` 硬编码上限，量级和 pending review 类似，本次不动）。
