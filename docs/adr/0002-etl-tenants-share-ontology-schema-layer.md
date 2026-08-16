# 结构化 ETL 租户复用本体 schema 层，不新建平行的 schema 定义机制

**Status**: proposed

MUJI（商品目录，`docs/MUJI_知识图谱_Schema设计方案_v6.md`）接入评估过程中，最初判断 MUJI 的 8 个关系应固定写在专属 ETL 模块的代码/配置里、不进 `tenant_relation_types`——理由是这些关系由数据源列映射确定性推出，不像 LLM 抽取场景那样需要业务自助增删改。

评估过程中确认：除 MUJI 外，已有明确计划接入第二个结构化主数据类型的租户，业务域与 MUJI 完全不同（设备/资产/工单类，而非商品目录），实体集合本身也不一样。这意味着"结构化 ETL 接入"不是 MUJI 的一次性需求，而是这个产品需要支持的一类通用能力——如果继续让每个 ETL 租户的实体/关系定义硬编码进各自的 Python 模块，等于每接入一个客户就要改一次代码，不构成"功能模块"。

因此改为：ETL 租户的实体类型、关系类型、domain/range 约束**复用 2026-08-14 本体 schema 基座计划已建好的 schema 定义层**（`ontology_categories.py`/`ontology_relations.py`/`ontology_constraints.py`/`ontology_lifecycle.py`，见 `docs/superpowers/specs/2026-08-14-ontology-schema-design.md`），通过同一套后台 UI 和 draft/confirm 生命周期定义。新增的 ETL 专属部分只有一个数据写入引擎（按已 confirm 的 schema + 列映射配置，做列→实体/关系的确定性写入），不是又一套 schema 定义表。

新增一个租户级"接入模式"（`ingestion_mode`：LLM 抽取 / ETL）标记，用于 `checkout_draft` 区分是否播种那 10 个为 LLM 抽取场景设计的通用默认关系——ETL 租户不播种，从空白草稿开始定义自己的关系集。

## Considered Options

- MUJI 关系硬编码进专属 ETL 模块代码（最初方案）：接入单个租户时足够，但接到第二个结构化租户时必须重构，属于"过早对单一场景做局部最优解"。
- 为 ETL 场景新建一套独立的 schema 定义表，与 `tenant_relation_types` 平行：能彻底隔离两种语义（LLM 发现 vs 确定性映射），但会产生两套本体管理界面和两套 draft/confirm 逻辑，维护成本更高，且两套定义之间没有实质的领域差异，只有"写入方式"不同。
- **（选定）复用现有 schema 层，新增 `ingestion_mode` 区分播种/写入行为**：一套 schema 管理界面覆盖两种数据来源，只在"要不要播种 LLM 默认值"这一个点上分支。

## Consequences

- `tenant_relation_types` 的 10 个默认值不再对所有租户无条件播种，`checkout_draft` 需要读取租户的 `ingestion_mode` 做分支。
- 后台 schema 管理 UI 需要对 ETL 租户呈现"从空白草稿开始定义关系"的引导，而不是"编辑/删除默认值"的引导——两种模式的首次使用体验不同。
- ETL 场景复用 `tenant_relation_types` 的全大写命名校验（`^[A-Z][A-Z0-9_]{0,63}$`），MUJI 文档里 `has_sku` 这类小写关系名注册时需转成 `HAS_SKU`，属于纯命名规范化，不影响语义。
- 新增的 ETL 写入引擎（暂命名 `schema_etl.py`）是本 ADR 之后需要单独设计/规划的模块，本 ADR 只确定它读取哪一层 schema、不确定它自己的实现细节。
- `schema_etl.py` 不是纯无状态的列→图转换：还需要一个通用的**稳定码注册机制**（原始值首次出现时分配 `node_key` 参与字段的稳定码，如 MUJI 的 `value_code`；以后重复出现时查到同一个码），供任何 ETL 租户复用，不只是 MUJI 专属——这个机制本身需要持久化存储（类似 `tenant_id + scope + raw_value → stable_code` 的注册表），是 `schema_etl.py` 设计里除了列映射配置执行之外的第二个核心能力。
