# Neo4j 知识图谱租户隔离设计方案

> 状态：设计定稿（经用户逐项确认）
> 背景：架构覆盖度审计发现 `app/graphrag/neo4j_client.py` 里所有 Cypher 模板完全没有 `tenant_id` 相关的过滤/标记，图谱事实上是单租户/全局共享的——不同租户客户的提问会检索到彼此的知识图谱内容。这是架构设计文档 §9"多租户与安全设计"明确要求但未落地的部分（原文："Neo4j 节点/关系统一打 tenant_id 属性，所有 Cypher 查询模板强制带租户过滤条件"）。前置条件已具备：`tenant_id` 已经从"客户端请求体自报"改为"网关注入的可信 Header"（见 `docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md`），现在做图谱层面的隔离是建立在可信输入上的，而不是在一个可被伪造的值上加一层形同虚设的过滤。

## 1. 隔离粒度决策

现状调查确认：术语表（`terminology.yaml`：标准名/别名/类型/产品线）目前是全局共享的单一配置文件，不区分租户，`product_line`（产品线）只是这份共享词表内部的分类维度，与"租户"是两个不同的概念。

**决策**：只隔离事实/关系边（`merge_relation` 写入的 `RELATED_TO`/`BELONGS_TO_MODULE`），术语节点（`:Term`）和别名边（`ALIAS_OF`）保持全局共享。真正需要隔离的是"租户 A 自己文档里 LLM 抽取出的关系事实不能被租户 B 检索到"，而不是术语本身的标准名/别名——这些是产品通用的知识基座，本来就该共享。与现有"共享术语表"架构完全兼容，改动面最小。

现有 Neo4j 中没有真实数据（此前实测确认 Neo4j 服务未启动，且图谱抽取当前不在默认摄取路径上），本次改动不需要设计迁移步骤，直接修改 Cypher 模板与调用签名即可。

## 2. 方案比较

**方案 A（采用）：关系边属性过滤**
在 `merge_relation` 写入的边上加 `tenant_id` 属性，查询时用 `WHERE` 子句强制过滤。与本项目 Milvus 向量库现有的隔离方式（metadata 字段过滤，非物理分库）是同一思路，也是架构文档 §9 原本设想的方式。改动面小，不涉及连接/驱动架构改造。

**方案 B（不采用）：每租户独立 Neo4j 数据库** —— Neo4j 4.0+ 支持多数据库，物理隔离更彻底，但需要企业版特性、且要重构当前单一驱动/会话的连接架构，对目前规模是明显的过度设计。

**方案 C（不采用）：租户前缀关系类型**（如 `RELATED_TO_T1`）—— 不可行：Cypher 关系类型不能参数化（`neo4j_client.py` 现有代码注释已说明这点），且会让类型白名单和语义组合爆炸。

## 3. 具体改动

### 3.1 `app/graphrag/neo4j_client.py`

- **`_SUBGRAPH_QUERY`**：加 `WHERE r.tenant_id = $tenant_id`。`ALIAS_OF` 边从不写 `tenant_id`，Cypher 里 `null = $tenant_id` 天然为假，会被这条过滤自动排除——不需要额外按关系类型区分，一条过滤条件同时做到"只看这个租户的事实边，别名边不受影响"。
- **`merge_relation`**：新增必填关键字参数 `tenant_id: str`，写入时 `SET r.tenant_id = $tenant_id`（与现有的 `r.source` 写在同一条 `SET` 子句里）。
- **`delete_relations_by_source`**：新增必填关键字参数 `tenant_id: str`，过滤条件从 `WHERE r.source = $source` 改为 `WHERE r.source = $source AND r.tenant_id = $tenant_id`。

  **这里顺带修复一个调查时发现的真实 bug**：现有实现只按 `source`（文档路径字符串，如 `docs/manual.md`）删边，完全不看租户。如果两个不同租户摄取了相同相对路径的文件，任一方重新摄取该文档时会把另一方写入的关系边也删掉（`extract_and_write_graph_relations` 在重新抽取前会先调这个方法清理旧边）。这是一个已经存在、与本次设计目标同源的跨租户数据破坏风险，本次一并修复。

- **`sync_term`/`sync_terms`/`_SYNC_TERM_QUERY`**：不改，术语节点和别名边保持全局共享（对应 3.1 节的隔离粒度决策）。

### 3.2 调用链改动

`tenant_id` 全部来自调用方已有的上下文（`AgentState["tenant_id"]`、摄取流水线的 `tenant_id` 参数），不需要新增数据来源，只是把已经存在的值继续往下传一层：

| 文件 | 改动 |
|---|---|
| `app/graphrag/normalization.py` | `GraphWriteClientProtocol.merge_relation` 签名加 `tenant_id`；`normalize_and_write_relations` 新增 `tenant_id` 参数并透传 |
| `app/ingestion/graph_extraction.py` | `extract_and_write_graph_relations` 新增 `tenant_id` 参数，透传给 `delete_relations_by_source` 和 `normalize_and_write_relations` |
| `app/ingestion/pipeline.py` | `_maybe_extract_graph_relations` 新增 `tenant_id` 参数并透传——外层 `_ingest_chunks` 已经持有 `tenant_id`（用于 `_embed_and_upsert`），只是此前没有继续往图谱抽取这条路径传 |
| `app/graphrag/term_guard.py` | `GraphClientProtocol.query_subgraph` 签名加 `tenant_id`；`build_term_guard_context` 新增 `tenant_id` 参数并透传 |
| `app/agent/graph.py` | `term_guard_node` 透传 `state["tenant_id"]`（已存在，只是没往下传） |
| `app/agent/tools.py` | `graph_query_tool` 新增 `tenant_id` 参数并透传 |
| `app/agent/planner.py` | `_dispatch_tool_call` 里对 `graph_query_tool` 的调用补上 `tenant_id`（`tenant_id` 在这个函数里已经存在，目前只用于 `vector_search_tool` 那一支） |

### 3.3 测试影响

以下测试文件的 mock/fake 调用点需要相应更新（补上 `tenant_id` 参数，断言里补上对应的过滤条件/属性）：
- `tests/graphrag/test_neo4j_client.py`
- `tests/graphrag/test_normalization.py`
- `tests/graphrag/test_term_guard.py`
- `tests/ingestion/test_graph_extraction.py`

新增测试点：
- `query_subgraph` 传入不同 `tenant_id` 时，只返回该租户写入的边（跨租户隔离的正面验证）。
- `delete_relations_by_source` 传入不同 `tenant_id`、相同 `source` 时，只删除对应租户的边（验证本次修复的那个 bug 确实修好了）。

## 4. 范围之外（不做）

- 不改动术语表（`terminology.yaml`）的加载/共享机制，`:Term` 节点和 `ALIAS_OF` 边保持全局共享。
- 不做历史数据迁移（现有 Neo4j 无真实数据）。
- 不引入 Neo4j 多数据库/物理分区架构。
- 不改动向量库（Milvus）层面的租户隔离（已有实现，不在本次范围）。
