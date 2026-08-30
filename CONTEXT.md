# customer_rag

企业产品/SaaS 客服问答机器人。核心是文档知识召回 + 基于知识图谱的专有名词准确率，服务多个租户，每个租户对应一个具体客户/业务场景。

## Language

**Term（术语）**:
知识图谱里的单一通用节点类型（Neo4j 标签固定为 `:Term`），承载业务专有名词——标准名、别名、分类、自由属性。所有租户共享同一套节点标签，靠属性和 `tenant_id`（关系边上）区分。**不是多类型实体体系**：`term_type` 只是这个唯一节点类型上的一个字符串分类属性，不同 `term_type` 的节点在物理结构（字段集合、身份键生成方式）上完全相同，不像 Product/SKU/VariantValue 那样各自有专属结构。
_Avoid_: Entity、Node（在讨论具体节点类型时容易和 Term 混淆）

**node_key（稳定身份键）**:
Term 即将新增的字段（见 [ADR-0003](docs/adr/0003-term-gets-stable-identity-key-separate-from-display-name.md)），取代 `standard_name` 成为 SQLite 主键和 Neo4j `MERGE` 的真正依据，创建后永不改变。`standard_name` 降级为普通展示属性，可随时修改而不触发特殊的改名处理——这是为了让 ETL 场景重复同步时不需要自己判断"改名 vs 新建"。LLM 抽取场景下 `node_key` 在创建时直接取当时的 `standard_name` 值，行为对现有用户无感。
_Avoid_: 身份码、value_code（那是 MUJI 文档里的叫法，本项目统一叫 node_key）

**node_key_template**:
`ontology_term_types` 上跟 `extra_fields` 平级的新字段，声明某个 term_type 的 `node_key` 由哪些字段、按什么模板拼接（如 `"Variant:{dim_code}:{value_code}"`）。所有 ETL 代码必须读这个模板来生成 `node_key`，不允许各自硬编码拼接规则——否则同一实体可能在不同 ETL 路径下算出不同的 `node_key`，重新引入 node_key 本该解决的"同一实体裂成两个节点"问题。

**value_type**:
`ontology_term_types.extra_fields` 上的每个字段声明，指定该 `Term.extra_properties` 字段值的类型（`"string"`/`"number"`/`"integer"`/`"number[]"`）。支持 ETL 场景的结构化数值/数组验证，不限于自由文本；拒绝 Python `bool` 值混入数值类型（因 bool 是 int 子类，否则会被静默接受）。

**term_type**:
Term 节点上的业务分类标签（如 "error_code"、"module"），描述这个术语*属于哪类业务概念*。原为全局枚举（`ontology_term_types`，不分租户），因 MUJI 接入需要把结构性实体类型（Product/SKU/Category/VariantValue 等）也塞进这个字段，已改为**按租户隔离**——见 [ADR-0001](docs/adr/0001-term-type-tenant-scoped-for-muji.md)。改动前的全局设计参见 `docs/superpowers/specs/2026-08-14-ontology-schema-design.md` 第 3 节。
_Avoid_: 分类、Category（容易和 Neo4j 概念里的 "Category" 实体混淆，见下）

**product_line（产品线）**:
2026-08-19 已从数据模型里彻底移除——每个租户实际只有一条产品线，这个字段形同摆设。详见 docs/superpowers/specs/2026-08-19-remove-product-line-design.md。

**抽取管道（extraction pipeline）**:
现有的知识图谱构建路径：LLM 从非结构化文档（PDF/Word/工单）里抽取专有名词间的关系，对齐到人工维护的术语表，写入前经过人工审核队列（`review_queue.py`）避免幻觉。
_Avoid_: 泛指的"数据管道"——必须点明这条路径是"LLM 推断 + 人工审核"，跟下面的 ETL 管道语义相反。

**（结构化）ETL 管道**:
为 MUJI 这类"真相源是干净关系型表"的租户新增的数据写入路径：从租户自己的主数据表（如 MUJI 的 `spu_products`/`sku_master`）确定性地转换、写入，不经过 LLM 推断，不进人工审核队列，因为没有幻觉风险。仍需同步写入 SQLite 的 Term 镜像层，保持与抽取管道一致的双存储架构。**Schema 定义本身复用现有的本体 schema 层**（`ontology_categories`/`ontology_relations`/`ontology_constraints`/`ontology_lifecycle`）——ETL 只负责按已 confirm 的 schema + 列映射配置做确定性写入，不另建一套平行的 schema 定义表。因为已确认存在多个业务域完全不同的结构化租户（MUJI 之外还有一个设备/资产/工单类租户在路上），必须做成通用模块，不能为每个租户各写一段硬编码 Python。

**本体库（ontology store）**:
`settings.graph_review_db_path` 指向的那个 SQLite 库，装着术语表、本体 schema（分类/关系类型/约束/接入模式）、两条审核队列、租户注册表、ETL 运行记录和稳定码注册表。它是知识图谱在 Neo4j 之外的镜像与治理层，两条接入管道都写它。
_Avoid_: review 库、审核库（审核队列只占其中两张表，这个叫法把它窄化了，也是 `review_factory.py`/`get_review_conn` 这些既有命名的来源）。注意 settings 里的字段名 `graph_review_db_path` 保持不变——它对应已经在用的环境变量，改名会破坏现有部署。

**接入模式（ingestion_mode）**:
租户级标记，区分该租户的知识图谱数据走"LLM 抽取"还是"结构化 ETL"路径。决定 `checkout_draft` 要不要播种那 10 个为 LLM 抽取场景设计的通用默认关系（ETL 租户不播种，从空白草稿开始定义自己的关系集）。当前设计里两种模式共享同一套 schema 定义层（分类/关系类型/约束/生命周期），只是数据写入路径和默认值不同。

**关系类型（relation type）**:
两个 Term 节点之间边的类型标签。无论 LLM 抽取还是 ETL 场景，都统一走 `tenant_relation_types` 表（租户可自助增删改、有 draft/confirm 两阶段生命周期，命名要求全大写）——这是 2026-08-15 的修正：最初曾考虑给 ETL 场景的关系单独固定写死在代码里，但确认存在多个业务域不同的 ETL 租户后，改为统一复用 schema 定义层，通过后台 UI 为每个租户各自定义/确认自己的关系集（如 MUJI 的 `has_sku`/`belongs_to_category` 会以 `HAS_SKU`/`BELONGS_TO_CATEGORY` 形式注册），不再需要每接入一个租户就改一次 Python 代码。

**记忆巩固（memory consolidation）**:
"抽取事实 -> 与已有记忆比对冲突 -> 执行记忆动作"这条核心链路（`app/memory/consolidation.py::consolidate_memory`），有两个触发时机：异步排队（对话结束后，`run_memory_consolidation` 委托调用）、同步即时（用户当轮明确表达"更正"意图，`app/agent/graph.py::correction_check_node` 直接调用）。两种触发时机共用同一条链路，不是两套独立实现。
_Avoid_: 记忆更新、冲突检测（这两个词只指链路里的某一步，不是整条链路本身）

**MUJI**:
接入中的一个新租户，商品目录场景。知识图谱需求是结构化商品目录问答（"有 500ml 以上的吗"这类数值范围过滤），跟其他租户的"从文档抽取专有名词"场景是完全不同的技术路径，但复用同一套 Term 节点体系和 Neo4j 实例，靠 tenant_id 隔离。设计方案见 `docs/MUJI_知识图谱_Schema设计方案_v6.md`。
