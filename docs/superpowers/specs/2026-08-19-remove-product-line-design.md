# 移除 product_line（产品线）概念 — 设计文档

## 背景

`product_line` 是术语（`terms`）的第二条分类轴，与 `term_type`（实体类型）并列：`term_type` 回答"这是什么类型的东西"，`product_line` 回答"这属于哪条产品线/业务线"。它贯穿数据模型、后台管理 UI、ETL 配置、结构化检索、LLM 上下文展示，是一个真实存在、有完整 CRUD 的功能面，不是孤立字段。

但确认后的实际使用情况是：**每个租户只有一条产品线**。本地开发库验证：只有一个租户（`default`），`ontology_product_lines` 表里只有一条值（"示例产品线"），2 条术语记录全部用的同一个值。这个字段在实际业务里从未起到过"区分"作用——它存在的唯一效果是给每一次创建/编辑实体、每一次 ETL 映射配置增加一个必填但毫无信息量的输入项。

决定：彻底从数据模型里删除这个概念，不是弱化/改成可选。

## 决策摘要（grill-me 已确认）

1. **terms 表的 `product_line` 列用原生 `ALTER TABLE ... DROP COLUMN` 直接删除**——项目实际使用的 SQLite 版本是 3.49.1，远超原生支持 DROP COLUMN 的 3.35.0 门槛；`product_line` 只是普通 `TEXT NOT NULL` 列，不是主键的一部分、没有 CHECK/UNIQUE 约束、不被任何生成列引用，符合原生语法的适用条件，不需要像早期迁移那样"建新表搬数据"。
2. **不做删除前的数据备份/审计日志**——这批数据本身就没有实际区分意义（每个租户历史上就一个值），备份它没有实际价值。
3. **Neo4j 图谱里历史 `:Term` 节点上已经写入的 `product_line` 属性不清理**——以后代码不再读也不再写这个属性，残留属性完全无害，批量遍历所有节点做 `REMOVE` 是有风险的额外迁移操作，收益极低，不做。
4. **ETL 配置文件（YAML）里 `EntityMapping.product_line` 直接从 schema 里删掉，不做旧格式兼容**——确认目前没有任何租户在实际运行 ETL，没有需要兼容的存量配置文件，直接硬切。
5. **agent 结构化过滤查询工具（`structured_filter_query.py`）里 `product_line` 直接摘除**——经重新核实，`product_line` 从未是 `AttributeConstraint.field` 可用的过滤维度（`_resolve_field_value_type` 只认 `_RESERVED_FIELD_NAME`="standard_name" 和该 term_type 声明的 `extra_fields`），它只出现在查询结果行的返回值里（跟 `term_type` 一样，是描述性字段，不是过滤条件）。删除它只是让查询结果行少一个 key，本来就不构成过滤能力的损失。

## 现状代码基线（写 plan 时的精确参照）

### 后端源码（非测试），逐文件列出所有 `product_line` 触点

| 文件 | 触点 |
|---|---|
| `app/graphrag/ontology.py` | `Term` dataclass 的 `product_line: str` 字段（第 16 行）；`Term.from_dict`（或等价加载函数）里 `product_line=str(item.get("product_line", ""))`（第 43 行） |
| `app/graphrag/terminology_seed.yaml` | 两条种子术语各有一行 `product_line: 示例产品线`（第 9、13 行）——种子文件本身也要删掉这两行 |
| `app/graphrag/ontology_categories.py` | `ontology_product_lines` 表定义（第 18 行起，**整表删除**）；`list_product_lines`/`create_product_line`/`update_product_line`/`delete_product_line` 四个函数（第 231-364 行区域，**整块删除**）——`update_product_line` 内部还级联 `UPDATE terms SET product_line = ?`（第 326 行），这条级联逻辑跟着函数一起删 |
| `app/graphrag/terms_store.py` | 触点最多的文件：`_SCHEMA_SQL`/迁移建表 SQL 里的 `product_line TEXT NOT NULL` 列（第 26、85 行）；`_bridge_seed_categories_from_existing_terms` 里读 `ontology_product_lines`/回填历史 `product_line` 去重值的逻辑（第 183、191、204 行区域）；`_row_to_term`（第 230 行）；`list_terms`/`get_term` 的 SELECT 列表（第 257、264、290 行）；`_validate_categories` 里 `product_line: str` 参数 + 校验分支（第 332、336、349-350 行）；`create_term`/`update_term`/`upsert_term_with_node_key` 三个写入函数的 `product_line: str` 参数与 SQL（第 375、391、398、405、423、438、448、454、497、534、541、544、552 行区域） |
| `app/api/admin_terms_routes.py` | `TermResponse.product_line`/`TermWriteRequest.product_line`（第 37、51 行，均为必填）；`field_validator("term_type", "product_line")`（第 65 行，改成只校验 `"term_type"`）；`_to_response`/三处 `create_term`/`update_term`/`Term(...)` 构造调用里的 `product_line=...`（第 84、135、151、188、224 行） |
| `app/api/admin_ontology_routes.py` | `create_product_line`/`delete_product_line`/`list_product_lines`/`update_product_line` 的 import（第 17-23 行）；`list_product_line_categories`/`create_product_line_category`/`update_product_line_category`/`delete_product_line_category` 四个路由（第 230-268 行区域，**整块删除**，包括对应的 Pydantic 请求体模型） |
| `app/graphrag/neo4j_client.py` | `_SYNC_TERM_QUERY` 里 `SET t.product_line = $product_line`（第 101 行，删除这个赋值，`$product_line` 参数也不再传）；`query_subgraph`（或等价函数）RETURN 子句里的 `anchor.product_line AS product_line`（第 278 行）；`sync_term` 方法里传给 Cypher 参数字典的 `"product_line": term.product_line`（第 396 行） |
| `app/graphrag/schema_etl_config.py` | `EntityMapping.product_line: str`（第 28 行，必填字段，**删除**）；YAML 解析里 `product_line=raw["product_line"]`（第 78 行） |
| `app/graphrag/schema_etl.py` | `_write_entity_mapping` 两处 `upsert_term_with_node_key(...)`/`Term(...)` 构造里的 `product_line=mapping.product_line`（第 115、120 行） |
| `app/graphrag/structured_filter_query.py` | `_CORE_TERM_FIELDS` frozenset 里的 `"product_line"` 成员（第 260 行，删除这一个元素，其余成员保留）；`run_structured_filter_query` 组装返回行时的 `"product_line": row["product_line"]`（第 303 行，整行删除） |
| `app/graphrag/term_guard.py` | 给 LLM 展示匹配术语上下文的那一行文本拼接，`产品线: {term.product_line}` 部分（第 92 行，删除这一段，只保留 `类型: {term.term_type}`） |
| `app/ingestion/main.py` | 第 80 行附近的注释提到 `type/product_line` 同步进图谱——如果这段注释在删除后仍然存在但字面不准确，需要同步改正 |

### 前端源码

- `frontend/src/admin/OntologySchemaPage.tsx`：整个"产品线 tab"（约第 1372-1520 行区域，含列表/新增/删除的完整 UI 和请求逻辑）需要整块删除；page 顶层 4 个 tab（实体类型/关系类型/约束/产品线）变成 3 个，tab 切换逻辑、类型定义（`ViewMode`/`isLifecycleTab` 等如果把 `product-lines` 算作一个 tab key）要同步收窄。
- `frontend/src/admin/TermsPage.tsx`（"实体列表"）：编辑表单里的产品线 `<select>`（连同加载 `productLineOptions` 的 fetch）、列表展示里的产品线文字（如果有）——全部删除。
- `frontend/src/admin/GraphReviewsPage.tsx`：内联创建实体表单里的产品线 `<select>`（连同 `productLineOptions` state/fetch）——删除，表单收窄成只有 `term_type` 一个必填字段。
- `frontend/src/admin/termsApi.ts`：`TermRecord.product_line: string`（必填字段）——删除。

### 测试文件（29 个，触及范围广，写 plan 时按模块分组处理，不是一个个单独枚举）

`product_line` 作为 `Term`/`create_term`/`TermRecord` 等核心构造函数的必填参数，被以下测试文件广泛用作夹具数据的一部分（不是被测试的行为本身，绝大多数只是"构造一条术语需要传这个字段"）：

`tests/graphrag/{test_schema_etl,test_review_queue,test_review_cli,test_normalization,test_terms_store,test_neo4j_client,test_ontology_categories,test_structured_filter_query,test_schema_etl_config,test_ontology,test_term_matcher,test_term_guard,test_graph_factory}.py`、
`tests/api/{test_admin_terms_routes,test_admin_graph_review_routes,test_admin_ontology_routes,test_admin_schema_etl_routes,test_admin_document_routes,test_voice_finalize_routes}.py`、
`tests/ingestion/{test_ingest_main,test_ingest_pipeline,test_graph_extraction}.py`、
`tests/agent/{test_tools,test_planner,test_graph_planner,test_graph}.py`、
`tests/qa/test_answer.py`、`tests/voice/test_asr_term_correction.py`、`tests/eval/test_terminology_accuracy.py`。

其中真正**测试 product_line 本身行为**（而非只是夹具传参）的测试，需要单独识别并删除/改写（不只是删掉参数）：
- `test_ontology_categories.py`：`create_product_line`/`update_product_line`/`delete_product_line`/`list_product_lines` 的直接单元测试——整块删除。
- `test_admin_ontology_routes.py`：产品线管理路由的测试——整块删除。
- `test_admin_terms_routes.py`/`test_terms_store.py`：可能有专门测试"未知产品线校验报错"的用例——删除。
- `test_structured_filter_query.py`：如果有断言查询结果行包含 `product_line` key 的用例，改成不包含。

## 不在本次范围内 / 已知限制

- 不做删除前的数据导出/备份（决策 2）。
- 不清理 Neo4j 历史节点上残留的 `product_line` 属性（决策 3）。
- 不做 ETL YAML 旧格式兼容（决策 4）。
- 如果未来真的出现需要多产品线区分的租户，需要重新设计——建议届时参照 `term_type` 现在的架构（租户级、草稿/确认生命周期），而不是简单地把这次删掉的全局枚举加回来。

## 验收标准（供写 plan/测试参考）

- 全文搜索 `product_line`（大小写不敏感）在 `app/`、`frontend/src/`（不含 `node_modules`）范围内应该没有残留引用，除了 Neo4j 历史属性（决策 3，代码层面不引用，数据层面允许残留）。
- `terms` 表结构不再包含 `product_line` 列；`ontology_product_lines` 表被删除。
- 本体 Schema 管理页面只剩 3 个 tab（实体类型/关系类型/约束）；「实体列表」「非结构化数据加工」两个页面的创建/编辑表单不再要求填产品线。
- ETL 配置 YAML schema 不再包含 `product_line` 字段；用一个真实的样例 YAML 跑一遍 ETL 确认不报错。
- 全量后端测试套件、`npx tsc --noEmit` 均通过。
