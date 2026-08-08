# 后台管理系统设计

> 状态：设计定稿（经用户逐项确认，via `/grill-me`）
> 背景：现有系统只有面向客户的问答 demo 前端（`frontend/`），文档摄取（`app/ingestion/`）和知识图谱人工审核（`app/graphrag/review_*`）都已经有完整的底层实现，但只暴露成 CLI/内部函数，没有 HTTP API，也没有任何前端。本设计要新增一个后台管理页面：上传/浏览/删除文档、知识图谱关系审核（含历史记录），通过前台导航栏的入口进入，登录保护，支持多租户切换。

## 0. 范围边界（明确不做）

- 不做"连接外部数据库读取文档"（比如客户自己的工单系统数据库）。这涉及存储任意数据库连接凭据、构造查询、SQL 注入/SSRF 风险，和"内部管理小工具"的定位不匹配，值得单独立项设计，不在这次范围内。
- 不做真正的多用户账号体系（没有"每个租户各自的管理员账号"，只有一个全局管理员 token）。
- 不做审核记录的编辑/撤销已批准的关系（只有待审核时可编辑标准名后批准，或驳回；已批准/已驳回是终态）。

## 1. 认证

- 后端从环境变量 `CUSTOMER_RAG_ADMIN_TOKEN`（沿用 `app/config/settings.py` 现有的 `CUSTOMER_RAG_*` 前缀约定）读一个写死的管理员 token。
- 登录：`POST /admin/auth/login` 请求体带这个 token，校验通过后签发一个短期有效（建议 8 小时）的 session token，写入内存/SQLite 的 session 表（记 token/签发时间/过期时间即可，不需要持久化到 Neo4j/Milvus）。
- 所有 `/admin/*`（除登录接口本身）都要求请求头带这个 session token（比如 `Authorization: Bearer <session_token>`），校验失败返回 401。
- 前端把 session token 存 `sessionStorage`（关标签页即失效，不做"记住登录"），未登录访问 `/admin/*` 路由重定向到 `/admin/login`。
- **全局管理员，不按租户区分账号**——登录后能看到/操作所有租户的数据，页面里用一个租户切换下拉框决定当前操作哪个租户，不是登录时选租户。

## 2. 多租户

- 后台管理支持多租户（不局限于当前唯一在用的 `demo`）。
- **`graph_review_queue` 表需要加 `tenant_id` 列**（现状完全没有这个字段，是全局共享表）：
  ```sql
  ALTER TABLE graph_review_queue ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'demo';
  ```
  用 `DEFAULT 'demo'` 迁移历史数据的原因：项目里目前唯一真实产生过数据的租户就是 `demo`，回填这个值比留空/回填 `unknown` 更准确、更少歧义。新记录由 `enqueue_for_review()` 调用方显式传入实际租户。
- 文档相关功能（`ingested_documents`、`ingestion_jobs`）本来就有 `tenant_id` 字段，不需要迁移。
- 管理页面（文档管理 + 图谱审核）顶部/侧边栏放一个共享的租户切换下拉框，切换后两个页面的数据都按新租户重新拉取。

## 3. 文档管理

### 3.1 功能范围

- **上传**：拖拽/选择文件，支持 `.md`/`.pdf`/`.docx`/`.png`/`.jpg`/`.jpeg`/`.csv`（工单格式），单文件最大 **100MB**，后端在接收阶段就校验大小（不是解析到一半才发现超限）。
- **浏览**：列表展示当前租户已摄取的文档（复用 `list_tracked_files()`），字段：文件名、chunk 数量、最近摄取时间；同时展示"处理中"的任务（复用 `list_pending_jobs()`）及其状态。
- **删除**：列表每条文档旁边删除按钮，后端组合调用 `vector_store.delete_by_source()` + `remove_tracked_file()`，两步都要成功才算删除完成（其中一步失败要有明确报错，不能留下"Milvus 里还有向量但追踪表已经没记录"这种不一致状态——具体容错策略实施时再细化，至少要求失败时把错误信息返回给前端，不能静默吞掉）。

### 3.2 上传流程（异步任务）

上传接口不直接同步跑完摄取（PDF/OCR + LLM 图谱构建可能耗时几十秒到几分钟，容易撞 HTTP 超时），走队列：

1. `POST /admin/documents` 上传文件 → 后端把文件内容写到磁盘（路径规划：`data/uploads/{tenant_id}/{uuid}_{原始文件名}`，避免同名文件冲突）→ 计算 content_hash → 调用 `enqueue_ingestion_job()` 入队，请求体/表单里带一个 `build_graph: bool` 勾选项 → 立即返回 `job_id`。
2. **`ingestion_jobs` 表需要加 `build_graph` 列**：
   ```sql
   ALTER TABLE ingestion_jobs ADD COLUMN build_graph INTEGER NOT NULL DEFAULT 0;
   ```
   `enqueue_ingestion_job()` 签名加 `build_graph: bool = False` 参数，一并写入。
3. `process_pending_jobs()` 循环体内改成按**每条任务自己的 `build_graph` 值**决定要不要把 `graph_llm_registry`/`graph_llm_provider_name`/`graph_terms`/`graph_client`/`graph_review_conn` 传给这条任务的 `_ingest_chunks()` 调用（这些资源本来就是外层一次性构建好传入 `process_pending_jobs()` 的，只是"用不用在这一条任务上"从"整批统一"改成"逐条判断" `if job["build_graph"]: ... else: 传 None`）。
4. 入队后，FastAPI 用后台任务（`BackgroundTasks` 或 `asyncio.create_task`）**立即**触发一次 `process_pending_jobs()`，不等外部 cron。现有的 `incremental_main.py` cron 任务继续保留、不冲突（`dedupe_key` 保证重复触发安全）。
5. 前端轮询 `GET /admin/jobs/{job_id}` 或复用 `GET /admin/documents` 里"处理中任务"列表的返回，展示状态（`pending`/`completed`/`dead` 及 `last_error`）。

## 4. 知识图谱审核

### 4.1 待审核列表

- `GET /admin/graph-reviews?tenant_id=xxx&status=pending`，复用 `list_pending_reviews()`（需要加 `tenant_id` 过滤参数）。
- 每条记录展示：候选实体名（subject/object）、关系类型、驳回原因分类（`reason`）、建议的标准名（`suggested_*_standard_name`，可能为空）。

### 4.2 批准/驳回

- **批准是一步到位的表单**，不是"先编辑再提交"两步：每条记录的 subject/object 标准名各是一个文本输入框，默认预填 `suggested_*_standard_name`（如果有），管理员可以直接确认或改成别的标准名再提交。如果 `suggested_*_standard_name` 为空（`reason` 是 `*_unresolved` 的情况），输入框留空，管理员必须手动填标准名才能提交（后端 `approve_review()` 本来就要求显式传入标准名，不支持留空）。
- `POST /admin/graph-reviews/{review_id}/approve`，请求体带最终确认的 `subject_standard_name`/`object_standard_name`，内部调用 `approve_review()`。
- `POST /admin/graph-reviews/{review_id}/reject`，请求体可选带 `resolved_note`，内部调用 `reject_review()`。

### 4.3 历史记录

- 新增查询函数 `list_resolved_reviews(conn, *, tenant_id, status=None, limit=50, offset=0)`（`app/graphrag/review_queue.py` 里现在没有这个函数，需要新写，表结构的 `status`/`resolved_at`/`resolved_note` 字段已经够用）。
- `GET /admin/graph-reviews?tenant_id=xxx&status=approved|rejected`，前端做成"待审核"/"历史记录"两个 tab（同一个页面内，不是单独的侧边栏项），历史记录 tab 内可再按 approved/rejected 筛选。

## 5. 前端

### 5.1 路由

- 引入 `react-router-dom`。路由表：
  - `/`：现有客服问答页（不变）
  - `/admin/login`：登录页
  - `/admin`：登录后默认跳文档管理页（`/admin/documents`）
  - `/admin/documents`：文档管理
  - `/admin/graph-reviews`：知识图谱审核（含待审核/历史 tab）
- 未登录访问 `/admin/*`（除 `/admin/login`）重定向到 `/admin/login`。

### 5.2 前台入口

- 导航栏（`App.tsx`，黄底黑边框）最右侧、"重新开始对话"按钮旁边，加一个同样样式的次级 outline 按钮："⚙️ 管理后台"，`<Link to="/admin">` 站内跳转（不开新标签页）。延续 `DESIGN.md` "不引入图标库，用文字+emoji" 的既有约定。

### 5.3 管理后台布局

- 左侧竖直侧边栏（黑边框直角，延续 brutalism 风格）：
  - "文档管理" / "知识图谱审核" 两个导航项
  - 底部：租户切换下拉框、"返回前台"链接、登出按钮
- 主体区域按当前路由渲染对应页面，延续 `paper`/`ink`/`shadow-brutal` 等现有 `DESIGN.md` token，不引入新的配色体系。
- 新组件预期：`AdminLayout.tsx`（侧边栏+主体框架）、`LoginPage.tsx`、`DocumentsPage.tsx`（上传表单+文档列表+处理中任务列表）、`GraphReviewsPage.tsx`（待审核/历史 tab + 批准表单）、可能还要一个通用的 `TenantSwitcher.tsx`。

## 6. 数据库迁移汇总

| 表 | 改动 | 原因 |
|---|---|---|
| `graph_review_queue` | 加 `tenant_id TEXT NOT NULL DEFAULT 'demo'` | 支持多租户隔离审核，历史数据回填到 `demo` |
| `ingestion_jobs` | 加 `build_graph INTEGER NOT NULL DEFAULT 0` | 支持逐任务决定是否触发图谱构建 |

两处都是加列（`ALTER TABLE ... ADD COLUMN`），不是破坏性迁移，不需要重建表。

## 7. 后端新增文件预估

- `app/api/admin_auth_routes.py`（登录/session 校验依赖）
- `app/api/admin_document_routes.py`（上传/列表/删除/任务状态）
- `app/api/admin_graph_review_routes.py`（待审核/历史/批准/驳回）
- `app/graphrag/review_queue.py` 新增 `list_resolved_reviews()`
- `app/ingestion/ingestion_queue.py` 修改 `enqueue_ingestion_job()`/`process_pending_jobs()` 支持 `build_graph`
- `app/ingestion/tracking.py`（如需要，补一个按 tenant 分页的查询包装）
- `app/main.py` 挂载新 router

## 8. 测试与验证

沿用项目现有方式：pytest 单元测试覆盖新增的队列/审核查询函数和迁移逻辑；前端 `npm run typecheck`；最终人工在浏览器里走一遍登录→上传→（等待任务完成）→查看文档列表→触发一条能进审核队列的摄取→批准/驳回→历史记录能看到结果，这几步全流程。
