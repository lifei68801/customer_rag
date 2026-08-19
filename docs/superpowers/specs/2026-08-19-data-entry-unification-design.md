# 三渠道数据填充统一设计 — 设计文档

## 背景

系统里实体和关系有三条真实存在的写入渠道：

1. **手工**（`/admin/terms`，术语库管理）——人工逐条创建/编辑实体；关系类型不支持手工创建，一直是空白。
2. **ETL 跑批**（`/admin/schema-etl`）——上传结构化数据文件，按已确认 schema 批量映射写入实体+关系；无需人工逐条确认。
3. **知识图谱审核**（`/admin/graph-reviews`）——LLM 从上传文档里抽取候选关系，人工逐条/批量审核批准后写入图谱。

上一轮工作（`docs/superpowers/specs/2026-08-19-term-type-draft-lifecycle-design.md`）把实体类型也接入了草稿/已确认生命周期，本体 schema 管理已经形成了一套完整、一致的确认机制。本文档要解决的是：schema 稳固之后，三条数据写入渠道之间彼此割裂、职责不清、甚至绕开 schema 约束的问题。

grill-me 过程中发现并确认的核心问题，分三类：

- **能力缺口**：知识图谱审核页遇到文档里提到但术语表里没有的实体时，`approve_review()` 直接抛 `StandardNameNotInTermsError`（`app/graphrag/review_queue.py:261`），审核员无法在页面内创建缺失的实体，只能中断去术语库管理页新建、再回来重试。
- **导航割裂**：三个功能各占一个平级侧边栏入口，互不知道彼此的存在，用户难以理解"这三个东西是同一份数据的三条入路"。
- **抽取脱离 schema**：知识图谱审核背后的 LLM 抽取（`app/graphrag/llm_extractor.py`）用的是硬编码的 10 种通用关系类型，完全不读取租户已确认的 `tenant_relation_types`；写入侧（`normalize_and_write_relations`）也不校验 relation_type/term_type 是否在租户确认范围内——"知识图谱抽取要基于预定义 schema"这个前提目前并不成立。

## 决策摘要（grill-me 已确认）

### A. 内联创建实体（知识图谱审核页）

1. 关系实例永远只能批量产生（ETL 或审核批准），术语库管理不新增关系创建 UI。
2. 审核页遇到未对齐到现有实体的候选时，走"内联快速创建实体"，复用现有 `create_term`（`app/graphrag/terms_store.py`）后端能力，不建立单独的候选实体审核队列。
3. 入口：`StandardNameInput.tsx` 现有建议下拉列表末尾追加"+ 创建为新实体"，仅当 standard_name 和别名均无匹配时出现。
4. 创建成功后立即重新拉取本页 `graphTerms`（`fetchGraphTerms`），刷新所有候选行的自动补全数据——避免同页多行引用同一新实体时的重复创建风险；不额外做"本页还有 N 条引用了这个名字"的主动提示。
5. 内联创建表单只要求 `term_type` + `product_line` 必填（下拉均只列 `status="confirmed"` 的选项），`aliases`/`extra_properties` 留空，后续可在"实体列表"页补充。
6. 提交前弹出确认框，展示即将创建的 `standard_name`/`term_type`/`product_line`，用户确认后才真正调用创建。
7. 创建成功后该行对应的 subject/object 旁加一个"新建"小标签，作为视觉区分。
8. 若提交时后端报 `TermNameConflictError`（撞名），确认框原地展示错误提示"该名称已存在，请刷新后从下拉列表中选择"，并自动重新拉取一次 `graphTerms`；不做额外的"直接选中已有项"快捷按钮。
9. LLM 抽取阶段已经会给候选 subject/object 各自输出一个受约束的 `term_type`（见 E 节），该值预填进内联创建表单的 term_type 下拉，审核员仍可改。

### B. 导航重组

1. 侧边栏「知识图谱审核」「ETL 跑批」「术语库管理」三个平级入口合并为一项，命名**「数据填充」**，与「本体 Schema 管理」（定义形状）形成对应关系（定义 schema → 填充数据）。
2. 「数据填充」内部用横向子 tab 切换三个原页面，默认落地在"实体列表"。三个子 tab 重命名为：
   - **实体列表**（原"术语库管理"，见 D 节的能力收窄）
   - **结构化数据加工**（原"ETL 跑批"）
   - **非结构化数据加工**（原"知识图谱审核"）
3. 子 tab 切换用可深链接的 URL 子路径：`/admin/data-entry/manual`、`/admin/data-entry/etl`、`/admin/data-entry/review`（默认重定向到 `manual`）。与 `OntologySchemaPage.tsx` 现有的纯 `useState` 草稿/确认 tab 切换不同——那三个原本各自独立的页面之前有独立 URL 可能被收藏/转发，合并后必须保留可深链接能力。
4. 旧路由 `/admin/terms`、`/admin/graph-reviews`、`/admin/schema-etl` 保留，用 `<Navigate replace>` 重定向到对应新子路径，避免旧书签变 404。

### C. 数据来源溯源（source 字段）

1. `terms` 表新增 `source` 列，枚举值 `manual` / `etl` / `review` / `unknown`，记录一条实体**最初**是通过哪个渠道创建的。
2. "实体列表"页的表格新增"来源"标签列，并新增"按来源筛选"下拉（全部/manual/etl/review/unknown），与现有 term_type/product_line 筛选并列。
3. 迁移历史数据：本次改动之前已存在的 `terms` 行统一回填 `source='unknown'`（无法追溯当时渠道，`unknown` 如实反映"迁移前无法确定"，不误导成"这些都是手动创建的"）。
4. **source 语义边界**：只记录创建时的渠道，后续任何人工编辑（改名/改别名/改属性，走 `update_term`）都不改变已有 source 值——`update_term` 的 UPDATE 语句不触碰 source 列。

### D. 移除独立的手动创建入口（重大结构调整）

1. **实体列表**（原术语库管理）收窄为纯浏览/搜索/编辑/删除，**去掉"新建实体"按钮和表单**。
2. 实体创建能力只保留两条路径：结构化数据加工（ETL）批量写入，或非结构化数据加工（审核页）内联创建。原因：单条手工录入表单本质是"一次导入一行"，ETL 完整覆盖了这个能力且更适合批量；单条创建作为独立入口价值有限，且内联创建已经复用了同一个 `create_term` 后端能力，不存在能力缺失。
3. 冷启动场景（新租户刚确认 schema，尚无文档、尚无 ETL 源文件）**也要求走 ETL**——哪怕只是准备一个只含几行种子数据的小文件——不设兜底的手动创建入口。

### E. 知识图谱抽取按本体 schema 收紧

现状（`app/graphrag/llm_extractor.py` + `app/graphrag/normalization.py`）：

- `_SYSTEM_PROMPT` 硬编码 10 种通用关系类型（RELATED_TO/PART_OF/IS_A/REQUIRES/ALTERNATIVE_TO/CAUSES/ADDRESSED_BY/LOCATED_IN/APPLIES_TO/PRECEDES），与租户的 `tenant_relation_types` 毫无关联。
- LLM 只输出 subject/object 的名字字符串，不输出类型。
- `normalize_and_write_relations()` 里有两条写入路径：
  - **AUTO_MERGED**：subject/object 都精确匹配到现有 `terms` 时，直接调用 `graph_client.merge_relation()` 写入图谱，不经人工审核。
  - **走审核队列**：未能精确匹配时才 `enqueue_for_review()`。
- `Neo4jGraphClient.merge_relation()`（`app/graphrag/neo4j_client.py:287`）对 `relation_type` 只做正则格式校验（`^[A-Z][A-Z0-9_]{0,63}$`）+ 保留名检查，**不校验是否在该租户已确认的 `tenant_relation_types` 里**。
- 没有任何地方检查 `is_ontology_confirmed()`；schema 未确认的租户也能正常触发文档抽取。

本次要改成：

1. **Prompt 侧动态拼接**：`_SYSTEM_PROMPT` 里硬编码的关系类型列表，改为调用方传入的该租户 `status="confirmed"` 的 `tenant_relation_types` 列表（`app/graphrag/ontology_relations.py::list_relation_types`）动态拼接进提示词。`extract_candidate_relations()` 新增入参接收这份列表。
2. **同时约束 term_type**：LLM 输出结构从 `{subject, object, relation_type, evidence}` 扩展为额外携带 `subject_type`/`object_type`，取值范围是该租户 `status="confirmed"` 的 `ontology_term_types`（`app/graphrag/ontology_categories.py::list_term_types`）。进一步用 `term_type_relation_allowlist`（`app/graphrag/ontology_constraints.py::list_allowed_combinations`，`status="confirmed"`）约束哪些 `(subject_type, relation_type, object_type)` 三元组组合是合法的——prompt 需要把这份允许组合表也拼进去，而不是把类型列表和关系列表分别罗列后让 LLM 自由排列组合。
3. **写入侧新增校验**：`normalize_and_write_relations()` 的 AUTO_MERGED 直写路径，新增校验——`relation_type` 必须在该租户已确认列表里、且 `(subject_term_type, relation_type, object_term_type)` 必须在 `list_allowed_combinations` 返回的允许组合里；不合规的候选**不再直接写图谱**，降级走 `enqueue_for_review()` 转人工审核（reason 用新值，例如 `not_in_confirmed_ontology`）。审核队列路径（原本就要人工确认）同样在 `approve_review()` 或其上游做同等校验，避免人工审核环节被绕过约束。
4. **schema 未确认时的闸值**：文档上传（存储+向量化）不受影响，仅跳过图谱抽取这一步——`extract_and_write_graph_relations()`（`app/ingestion/graph_extraction.py`）调用前先查 `is_ontology_confirmed()`，未确认则记日志并跳过，前端提示"schema 未确认，本次上传未抽取知识图谱"。
5. **候选类型回填内联创建表单**：审核队列新增列存储 LLM 给出的 `subject_type`/`object_type` 候选值（已经是从确认范围内选出的合法值），内联创建表单据此预填 term_type 下拉，审核员可改。

## 现状代码基线（写 plan 时的精确参照）

| 模块 | 文件 | 相关点 |
|---|---|---|
| 实体创建 | `app/graphrag/terms_store.py::create_term` | 内联创建/ETL 复用的唯一创建入口；校验 term_type/product_line 为 `status="confirmed"` |
| 实体编辑 | `app/graphrag/terms_store.py::update_term` | source 列不参与本次改动的更新范围 |
| 审核批准 | `app/graphrag/review_queue.py::approve_review`（第 261 行起） | 校验 subject/object 必须在 `terms` 里，否则 `StandardNameNotInTermsError` |
| 审核队列表 | `app/graphrag/review_queue.py` 的 `_SCHEMA_SQL`（第 12 行起） | 现有列：`subject_candidate`/`object_candidate`/`relation_type`/`reason`/`suggested_subject_standard_name`/`suggested_object_standard_name`/`status`/`source`/`evidence`；本次需新增 `subject_type_candidate`/`object_type_candidate` |
| LLM 抽取 | `app/graphrag/llm_extractor.py::extract_candidate_relations` | `_SYSTEM_PROMPT` 硬编码 10 类关系；返回值不含类型字段 |
| 归一化写入 | `app/graphrag/normalization.py::normalize_and_write_relations` | AUTO_MERGED 直写分支（第 156-177 行）与转审核分支并存，是本次新增校验的落点 |
| 抽取触发 | `app/ingestion/graph_extraction.py::extract_and_write_graph_relations` | 已持有 `tenant_id`，是接入 schema 确认检查、动态关系类型列表的位置 |
| 关系类型读取 | `app/graphrag/ontology_relations.py::list_relation_types(conn, tenant_id, *, status)` | 沿用现有签名，取 `status="confirmed"` |
| 实体类型读取 | `app/graphrag/ontology_categories.py::list_term_types(conn, tenant_id, *, status)` | 沿用现有签名，取 `status="confirmed"` |
| 允许组合读取 | `app/graphrag/ontology_constraints.py::list_allowed_combinations(conn, tenant_id, *, status)` | 返回 `AllowedCombination(subject_term_type, relation_type, object_term_type)` 列表 |
| 图谱写入校验 | `app/graphrag/neo4j_client.py::merge_relation`（第 287 行起） | 现有正则+保留名校验保留不变，新增的确认范围校验加在调用方（`normalize_and_write_relations`），不动这个函数本身 |
| 本体确认状态 | `app/graphrag/ontology_lifecycle.py::is_ontology_confirmed` | 判断依据不变（只查 `tenant_relation_types` 已确认存在），本次只是新增一个调用点 |
| 术语表结构 | `app/graphrag/terms_store.py` 的 `_SCHEMA_SQL`（第 20 行起） | 新增 `source` 列 |
| 自动补全组件 | `frontend/src/admin/StandardNameInput.tsx` | 新增"+ 创建为新实体"列表项 |
| 审核页 | `frontend/src/admin/GraphReviewsPage.tsx` | 内联创建表单、"新建"标签、`graphTerms` 刷新逻辑 |
| 实体列表页 | `frontend/src/admin/TermsPage.tsx` | 去掉创建表单，加来源列+筛选 |
| 侧边栏导航 | `frontend/src/admin/AdminLayout.tsx`（第 32-46 行） | 三项合并为一项"数据填充" |
| 路由表 | `frontend/src/App.tsx`（第 16-22 行） | 新增 `/admin/data-entry/*` 子路由 + 旧路径重定向 |

## 不在本次范围内 / 已知限制

- **ETL 的 `_write_entity_mapping`/`_write_relation_mapping` 是否也要经过同等的确认组合校验**：ETL 本身已经 gate 在 `is_ontology_confirmed()` 之上（写入时按已确认 schema 校验 term_type/product_line），关系写入是否也要过 `term_type_relation_allowlist` 校验，留给写 plan 时按现状代码确认（大概率现状已经隐式满足，因为 ETL 的映射配置本身就是照着已确认 schema 填的）。
- **ETL 实体写入遇到已存在 standard_name 时是 update 还是报冲突**：本次讨论中止于此问题，写 plan 前需要先读 `schema_etl_row_processing.py::_write_entity_mapping` 现状代码确认，不在本文档里假设结论。
- **node_key/stable_code 与 term_type 改名的耦合**（`schema_etl_row_processing.py::compute_node_key`/`allocate_stable_code`）——上一轮 spec 已记录为已知限制，本次不修复。
- **迁移工具（"迁移实体类型/关系类型"）的旧类型行选择依赖当前可见草稿行**这一预置限制——上一轮 spec 已记录，本次不修复。
- **relation_type 迁移改名后审核队列里已挂起的候选行**是否需要联动更新——现有候选行存的是抽取时的 relation_type 快照，本次不处理这类历史候选与后续改名的联动。

## 验收标准（供写 plan/测试参考）

- 知识图谱审核页，候选 subject/object 未匹配任何现有实体时，`StandardNameInput` 出现"+ 创建为新实体"，创建成功后该行与同页其余引用同名候选的行都能重新搜到这个实体。
- `terms` 表所有行都有非空 `source`；历史数据为 `unknown`；新建的行按创建渠道分别是 `manual`（若彻底移除后应不再产生）/`etl`/`review`；人工编辑已有行不改变其 `source`。
- 实体列表页不再提供"新建实体"入口；按来源筛选可用。
- 侧边栏只剩「本体 Schema 管理」「文档管理」「数据填充」等入口（不再有独立的知识图谱审核/ETL 跑批/术语库管理三项）；`/admin/data-entry/{manual,etl,review}` 均可直接访问；旧路径重定向生效。
- 未确认 schema 的租户上传文档：文档正常入库+向量化；图谱抽取步骤被跳过，无候选进入审核队列。
- 已确认 schema 的租户：抽取出的候选 relation_type 只会是该租户已确认的类型；AUTO_MERGED 直写路径对不在允许组合内的候选不再直接写图谱，而是转人工审核。
