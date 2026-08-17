# ETL 管理后台触发界面设计

**状态**：设计定稿，待写执行计划。
**上游依赖**：`docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md`（`schema_etl.py` 写入引擎，本设计给它加一个 HTTP 触发入口）；`docs/superpowers/plans/2026-08-16-schema-etl-engine.md`（`ETLRunReport`/`run_schema_etl` 的实际接口）。

## 0. 问题陈述

`app/graphrag/schema_etl.py::run_schema_etl` 及其 CLI 入口（`--config`/`--data-dir` 参数）已经完整实现，但没有任何 HTTP 接口或管理后台页面能触发它——业务方要跑一次 ETL，只能有服务器 shell 权限的人手动执行命令行。这与本项目其它数据写入路径（文档摄取走 `admin_document_routes.py` 的上传接口、术语表走 `admin_terms_routes.py` 的增删改接口）不一致，也不现实——不是每个需要触发 ETL 的业务方都应该有服务器 shell 权限。

## 1. 触发方式：浏览器上传 YAML 配置 + CSV 数据文件

管理后台新增一个页面，流程：

1. 选择/确认租户。
2. 上传列映射 YAML 配置文件。
3. 上传一个或多个 CSV 数据文件（文件名需要与 YAML 里各 `EntityMapping`/`RelationMapping` 声明的 `source_file` 对应）。
4. 点击"开始运行"。
5. 后台异步执行，前端轮询状态，完成后展示报告。

**不做**：不支持引用服务器本地路径（避免路径合法性校验、目录遍历风险，且与文档上传的既有交互模式保持一致）。

## 2. 执行模型：复用 `BackgroundTasks`，不引入新的任务队列系统

与 `app/api/admin_document_routes.py::upload_document` 的既有模式一致——用 FastAPI 的 `BackgroundTasks` 在同一进程里异步跑，不引入 Celery/RQ 等外部队列系统。理由：ETL 跑批是"整批次性任务"（一次跑完就结束，不像文档摄取队列那样有 retry/dead 等多状态流转），继续沿用现有基础设施足够，不构成 YAGNI 违反。

## 3. 状态与报告持久化：新建 `etl_runs` 表

不复用 `ingestion_jobs` 表——那张表的字段（`content_hash`/`build_graph` 等）是为文档摄取设计的，ETL 跑批的数据形状不同（一次运行对应一份完整的 `ETLRunReport`，不是单文件的摄取状态），强行复用会引入不相关字段或需要 `ALTER TABLE`。

```sql
CREATE TABLE IF NOT EXISTS etl_runs (
    run_id       TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',  -- running | completed | failed
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    report_json  TEXT,  -- ETLRunReport 序列化（dataclasses.asdict + json.dumps）
    error        TEXT   -- status='failed' 时记录未被 ETLRunReport 覆盖的异常信息
                          -- （如 SchemaETLNotConfirmedError、文件读取失败等运行前/运行中的意外）
);
CREATE INDEX IF NOT EXISTS idx_etl_runs_tenant_status ON etl_runs (tenant_id, status);
```

`report_json` 存完整的 `ETLRunReport`（含全部 `skipped_rows`/`skipped_mappings`，不做截断）——SQLite 的 `TEXT` 列没有实际大小限制，截断逻辑放在第6节的展示层，不在存储层做。

## 4. 并发控制：同一租户同时只允许一次 `running` 的 ETL 跑批

`app/graphrag/etl_stable_code_registry.py::allocate_stable_code` 的文档明确写着"假设同一租户的 ETL 任务串行执行，查询命中判断与插入之间没有加锁"——这是已确认的既有假设（`docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md` 第3.3节），本设计不引入分布式锁去打破它，而是在触发入口这一层守住这个假设：

启动新跑批前，先查 `etl_runs` 表该租户有没有 `status='running'` 的记录，有则直接拒绝（HTTP 409），前端提示"该租户已有 ETL 任务在运行，请等待完成后再试"。

**已知的竞态窗口**（记录不修复，接受为可容忍风险）：两个几乎同时的请求都查到"没有 running 记录"、都尝试插入 `status='running'` 的新行——理论上存在极小的竞态窗口。缓解：`etl_runs` 表可以给 `(tenant_id) WHERE status = 'running'` 建一个部分唯一索引（SQLite 支持 `CREATE UNIQUE INDEX ... WHERE status = 'running'`），第二个插入会因为唯一约束直接失败，转成对该请求返回 409——比纯查询+插入的检查再加一层真正的数据库层保证，且实现成本很低，实施计划阶段直接采用。

## 5. 前置校验：页面加载即查 schema 确认状态

新增一个 `GET /api/admin/{tenant_id}/schema-etl/status` 接口，返回 `{"ontology_confirmed": bool}`（内部调用已有的 `is_ontology_confirmed`）。前端页面加载时先调这个接口：未确认则禁用上传/运行入口，提示"该租户 schema 尚未确认，请先完成本体 schema 确认"（可以附一个跳转到本体管理页面的链接，具体页面路径以实施计划阶段的实际路由为准）。

这只是 UX 优化，不是安全边界——`run_schema_etl` 自己仍然会在真正执行时再检查一次 `is_ontology_confirmed` 并拒绝（`SchemaETLNotConfirmedError`），双重检查之间的竞态（页面加载后、点击运行前，schema 被别人改成未确认）由后端这层兜底，不需要额外处理。

## 6. 报告展示：总览全显示，跳过明细预览+下载

报告页面（跑批完成后，或历史记录里点开一条 `completed`/`failed` 记录）布局：

1. **总览**：`entities_written`/`entities_skipped`/`relations_written`/`relations_skipped` 四个数字。
2. **按类型统计表格**：`written_by_type`/`skipped_by_type` 两个 dict 合并成一张表，每行一个 `term_type`/`relation_type`，两列写入数/跳过数。
3. **跳过明细预览**：`skipped_rows`（按 `SkippedRow` 的 `label`/`source_file`/`row_number`/`reason` 四列展示）只渲染前50条，超过50条时提示"还有 N 条，点击下载完整报告查看"。`skipped_mappings`（映射级跳过，通常条数很少）全部显示，不做截断。
4. **完整下载**：一个下载按钮，命中新增的 `GET /api/admin/{tenant_id}/schema-etl/runs/{run_id}/report.csv` 接口，把该次运行的 `skipped_rows` 转成 CSV 流式返回（`label,source_file,row_number,reason` 四列表头）。`failed` 状态的运行没有 `report_json`（运行前/运行中就失败了），下载接口对这种情况返回 404，前端不显示下载按钮。

## 7. 文件留存：不自动删除

上传的 YAML/CSV 文件复用 `deps.get_upload_dir()` 的既有基础设施，存到 `{upload_dir}/schema-etl/{tenant_id}/{run_id}/` 下，跑完之后不自动清理——与现有文档上传的留存策略一致，便于事后审计/重跑；磁盘占用问题留到有实际证据时再处理（YAGNI，与文档上传目前的现状一致）。

## 8. HTTP 接口

新增 `app/api/admin_schema_etl_routes.py`，路由前缀 `/api/admin/{tenant_id}/schema-etl`，`dependencies=[Depends(deps.require_admin_session)]`（与其它 admin 路由文件一致）：

| 方法+路径 | 作用 |
|---|---|
| `GET /status` | 返回 `{"ontology_confirmed": bool}`（第5节） |
| `POST /runs`（multipart：`config` 文件 + `data_files` 多文件） | 保存上传文件、插入 `etl_runs` 行（`status='running'`）、`background_tasks.add_task` 异步跑 `run_schema_etl`，立即返回 `{"run_id": ...}` |
| `GET /runs` | 列出该租户全部历史跑批（`run_id`/`status`/`started_at`/`finished_at`，不含完整 `report_json`，供列表页展示） |
| `GET /runs/{run_id}` | 返回该次跑批的完整详情，含 `report_json` 反序列化后的结构（供报告页面渲染第6节的1-3项） |
| `GET /runs/{run_id}/report.csv` | 下载完整 `skipped_rows` CSV（第6节第4项） |

## 9. 前端

新增 `frontend/src/admin/SchemaEtlPage.tsx`，在 `App.tsx` 里挂到 `<Route path="schema-etl" element={<SchemaEtlPage />} />`（`AdminLayout` 下，与 `documents`/`terms` 平级）。页面结构参照 `DocumentsPage.tsx` 的上传+轮询模式：

- 页面加载：调 `GET /status`，未确认则禁用表单，显示提示。
- 上传表单：YAML 文件选择 + CSV 多文件选择 + "开始运行"按钮。提交后调 `POST /runs`，拿到 `run_id` 后开始轮询 `GET /runs/{run_id}`（沿用文档摄取页面现有的轮询间隔/停止条件写法）。
- 历史记录列表：调 `GET /runs`，展示过往跑批，点击一条跳转/展开到报告详情（第6节布局）。
- 并发拒绝（409）：轮询到已有 `running` 记录，或点击"开始运行"时后端返回 409，都用同一条提示文案。

## 10. 范围外事项

- 不支持定时/周期性 ETL 触发——本设计只做手动触发，定时调度留给未来有真实需求时评估。
- 不支持取消一个正在运行的跑批——`run_schema_etl` 目前没有取消机制，加这个需要改造执行引擎本身，不在本次范围。
- 不做跨租户批量触发——每次操作限定单一租户，与现有管理后台"选定租户后操作"的交互模式一致。
