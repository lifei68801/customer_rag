# ETL 驱动的知识图谱 Schema 构建——架构设计

> 状态：设计定稿（经 grill-with-docs 逐题确认）
> 前提：在 [2026-08-14《知识图谱本体 Schema 设计》](2026-08-14-ontology-schema-design.md)（下称"本体基座"）之上扩展，不推翻其结论，只新增一条数据接入路径。
> 触发背景：评估 `docs/MUJI_知识图谱_Schema设计方案_v6.md`（商品目录知识图谱）接入现有客服系统的可行性时确认——除 MUJI 外，已有明确计划接入第二个"结构化主数据"类型的租户，业务域与 MUJI 完全不同（设备/资产/工单类）。因此结论从"给 MUJI 写一次性 ETL 脚本"升级为"产品需要一个通用的、可复用于任意结构化主数据租户的 ETL 构建知识图谱 schema 功能模块"。
> 关联 ADR：[0001](../../adr/0001-term-type-tenant-scoped-for-muji.md)（term_type 按租户隔离）、[0002](../../adr/0002-etl-tenants-share-ontology-schema-layer.md)（ETL 租户复用本体 schema 层）、[0003](../../adr/0003-term-gets-stable-identity-key-separate-from-display-name.md)（Term 增加 node_key）。
> 关联术语表：[CONTEXT.md](../../../CONTEXT.md)。

---

## 0. 这份文档解决什么问题

`docs/ARCHITECTURE.md` 定义的知识图谱构建方式是"人工术语表 + LLM 抽取双轨制"——面向非结构化文档（PDF/Word/工单）。这个假设对 MUJI 这类真相源本身就是干净关系型表（`spu_products`/`sku_master` 等）的租户不成立：LLM 推断在这里既无必要（没有幻觉风险要防）也无意义（结构已经确定，不需要"发现"）。

评估 MUJI 接入过程中，最初倾向于给 MUJI 写一条专属的、硬编码的 ETL 脚本。但确认存在第二个业务域完全不同的结构化租户后，这个方向被推翻——如果每接入一个结构化租户都要改一次 Python 代码，就不构成"产品能力"，只是重复劳动。本文档给出的架构目标是：**新增一条"结构化 ETL"数据接入路径，与现有"LLM 抽取"路径共享同一套 schema 定义层和 Term 节点体系，仅在数据写入方式、默认值和身份标识上分叉。**

**需要明确区分的两件事**（容易被"ETL 驱动的 schema 构建"这个标题混淆）：本文档里"ETL"指的始终是**按已确认 schema 写入数据**（第 7 节 `schema_etl.py`），不是"用 ETL 抽取的数据反过来推导/生成 schema"。Schema 本身永远是第 5 节所述的、人工在后台预先定义并确认的产物——这是继承自本体基座"schema-first 强制门禁"的核心原则，ETL 场景不例外。是否需要额外的"数据驱动 schema 建议工具"（分析源表统计，辅助业务决定怎么定义 schema）已评估并决定不做，见第 9 节。

---

## 1. 现状与复用点

| 组件 | 现状 | 本设计的处理方式 |
|---|---|---|
| `app/graphrag/ontology_categories.py`/`ontology_relations.py`/`ontology_constraints.py`/`ontology_lifecycle.py`（本体基座） | 服务 LLM 抽取场景的 schema 定义层：分类/关系类型/约束 + draft/confirm 生命周期 | **两种接入模式共用同一套**，不新建平行表（第 5 节） |
| `app/graphrag/terms_store.py` + `app/graphrag/neo4j_client.py`（Term 体系） | 单一节点类型 `:Term`，身份键=展示名（`standard_name`） | 改造身份模型（第 3 节），两种接入模式共用同一套节点体系 |
| `app/ingestion/*`（文档解析→分块→LLM 抽取管道） | 仅服务非结构化文档来源，`review_queue.py` 做人工审核兜底 | **不涉及**，结构化 ETL 是完全独立的写入路径，不复用这条管道 |
| `app/agent/tools.py::graph_query_tool` | 仅支持"给定标准名 → 查关联子图"的单一模式 | 需要新增结构化过滤查询能力（第 8 节） |

---

## 2. 两种数据接入模式并存

新增租户级标记 `ingestion_mode`：`extraction`（LLM 抽取，现状默认）/ `etl`（结构化确定性写入）。

```sql
CREATE TABLE tenant_ingestion_config (
    tenant_id      TEXT PRIMARY KEY,
    ingestion_mode TEXT NOT NULL DEFAULT 'extraction',  -- 'extraction' | 'etl'
    created_at     TEXT NOT NULL
);
```

**这个标记目前唯一的行为分支点**：`ontology_lifecycle.py::checkout_draft` 检出草稿时，只有 `ingestion_mode='extraction'` 的租户才播种本体基座里那 10 个为 LLM 抽取场景设计的通用默认关系（`seed_default_relation_types`）；`ingestion_mode='etl'` 的租户从空白草稿开始，自己定义关系集（如 MUJI 的 `HAS_SKU`/`BELONGS_TO_CATEGORY`……）。

除此之外，两种模式下的 schema 编辑体验（后台 UI、CRUD、draft/confirm）完全一致——分叉点只在于"要不要给一份不相关的默认参考"。

---

## 3. Term 核心模型改造：node_key

### 3.1 现状问题

`Term` 当前的身份键就是展示名 `standard_name`：`terms_store.py` 里是 SQLite 主键，`neo4j_client.py` 里是 Neo4j `MERGE` 的匹配字段（`MERGE (t:Term {standard_name: $standard_name})`）。改名要走专门的 `rename_term_node` 接口（`MATCH` 旧名 → `SET` 新名），这是为人工在后台点一次"改名"按钮设计的操作。

结构化 ETL 场景需要重复、增量地把源表数据同步进图谱（MUJI 一张表就有 186,198 行 SKU），源系统里的展示文本随时可能变。如果身份键就是展示名，ETL 每次重跑都要自己判断"这行是新建还是改名"，判断错了会导致同一实体在图里裂成两个节点——这正是 MUJI 文档 v5→v6 改版要解决的"改名断边"问题。

### 3.2 设计：拆分身份键与展示名

```
Term:
  node_key          string   稳定身份键。SQLite 主键、Neo4j MERGE 匹配字段。
                              创建后永不改变，不受任何"改名"操作影响。
  standard_name     string   展示名。普通属性，可随时修改，不触发特殊处理。
                              仍需唯一性约束（业务要求同一展示名不能被两个术语占用），
                              但不再是物理主键。
  aliases           string[]
  term_type         string   （见第 4 节，改为按租户隔离）
  product_line      string   （维持全局，不受本次改造影响）
  extra_properties  object   （见第 6 节，值类型松绑）
```

**两种接入模式下 `node_key` 的来源不同**：
- `extraction` 模式：没有外部系统提供稳定码，`node_key` 在创建时直接取当时的 `standard_name` 值，此后固定不变——对现有用户几乎无感，今天的"改名"操作效果上变成只更新 `standard_name` 这一个展示属性。
- `etl` 模式：`node_key` 由 ETL 引擎按 `node_key_template`（第 5.3 节）从源数据拼接而来。

### 3.3 对现有代码路径的影响

- `terms_store.py`：`terms` 表主键从 `standard_name` 改为 `node_key`；`standard_name` 改为带唯一索引的普通列。
- `neo4j_client.py`：`sync_term`/`merge_relation`/`query_subgraph`/`rename_term_node`/`delete_term_node` 等所有按 `standard_name` 匹配节点的 Cypher，改成按 `node_key` 匹配；`rename_term_node` 语义从"改变匹配键"变成"更新一个普通展示属性"，理论上比现在的模式更安全（不再要求调用方知道当前准确展示名才能定位节点）。
- 存量数据迁移：`terms` 表现有每一行，`node_key` 直接回填当时的 `standard_name` 值（`extraction` 模式的既定行为，见 3.2），对已有数据和查询行为无影响。

### 3.4 索引要求

本设计的"多类型实体"是靠 `term_type` 取值 + `node_key_template` + 类型化 `extra_fields` 组合模拟出来的（第 5 节），不是 Neo4j 原生的多标签设计——所有节点共享同一个 `:Term` 标签，这意味着按 `term_type` 过滤没有标签可用，只能靠属性匹配。MUJI 的 SKU 一个 term_type 就有 186,198 行，没有索引的话任何"这个类型下有哪些节点"的查询都会退化成全表扫描。

**必须建立的属性索引：**

```cypher
CREATE INDEX FOR (t:Term) ON (t.term_type);
CREATE INDEX FOR (t:Term) ON (t.node_key);
```

MUJI 文档 §4 要求的数值比较类索引（`numeric_value`/`dims`/`order_rank`）建在 `extra_properties` 内部字段上——这类值目前存储在一个属性 map 里，Neo4j 对 map 内部字段建索引有限制，具体是否需要把这些字段提升为节点顶层属性才能建索引，留待第 8 节结构化过滤查询工具的实施阶段一并确定。

---

## 4. term_type 分类改为按租户隔离

详细论证见 [ADR-0001](../../adr/0001-term-type-tenant-scoped-for-muji.md)，此处摘要影响面：

- `ontology_term_types` 表新增 `tenant_id` 列并调整主键为 `(tenant_id, value)`。
- `ontology_categories.py` 的 CRUD、级联改名（第 3.2 节所述的“改名不影响身份”与此处的“改名级联到 terms/约束表”是两回事，互不冲突）、delete-protection 逻辑全部按租户过滤。
- `admin_ontology_routes.py` 的 term-types 路由从全局路径（`/term-types`）改成按租户路径（`/{tenant_id}/term-types`），与 relation-types/constraints 路由风格对齐。
- `product_line` **不受影响**，维持全局枚举——MUJI 场景没有对应需求，没有理由跟着改。
- 存量数据迁移：现有全局 `term_type` 行（如 `error_code`/`module`）需要决定归属到哪个/哪些租户，或引入一个共享默认租户兜底，避免迁移后老租户丢失已有分类——具体方案留待实施计划阶段确定（第 10 节）。

---

## 5. Schema 定义层：两种接入模式共用，不新建平行表

详细论证见 [ADR-0002](../../adr/0002-etl-tenants-share-ontology-schema-layer.md)。核心结论：`etl` 模式租户的实体类型、关系类型、domain/range 约束，走**与 `extraction` 模式完全相同**的 schema 定义层（`ontology_categories`/`ontology_relations`/`ontology_constraints`/`ontology_lifecycle`），通过同一套后台 UI 和 draft/confirm 生命周期定义，不是又建一套平行机制。

### 5.1 实体类型 = term_type（按租户隔离后）

MUJI 的 `Product`/`SKU`/`Category`/`VariantValue`/`Series`/`Material`/`Season`/`Origin`/`TargetGender` 各自注册为 MUJI 租户下的一个 `term_type` 值。第二个结构化租户（设备/资产/工单类）注册自己完全不同的一套 `term_type` 值——两者互不可见，靠第 4 节的按租户隔离保证。

### 5.2 关系类型：统一走 `tenant_relation_types`

MUJI 的 8 个关系（`has_sku`/`belongs_to_category`/`contains_child`/`has_variant`/`has_material`/`part_of_series`/`in_season`/`from_origin`/`suitable_for_gender`）在后台 UI 里以 `HAS_SKU`/`BELONGS_TO_CATEGORY`/……的形式注册——本体基座的关系类型命名校验（`^[A-Z][A-Z0-9_]{0,63}$`）要求全大写，MUJI 文档里的小写 snake_case 命名属于纯展示风格差异，注册时规范化即可，不影响语义。

domain/range 约束表 `term_type_relation_allowlist` **已经支持**跨类型组合——`_validate_references` 独立校验 `subject_term_type` 和 `object_term_type`，不要求两者相同，因此 `(Product, HAS_SKU, SKU)` 这类异构类型组合结构上直接可用，不需要改造。

### 5.3 新增字段：`node_key_template`

`ontology_term_types` 在 `extra_fields` 之外新增 `node_key_template` 字段，跟随 `term_type` 定义一起声明该类型的 `node_key` 由哪些字段、按什么模板拼接：

```
term_type = "VariantValue"
node_key_template = "Variant:{dim_code}:{value_code}"

term_type = "Product"
node_key_template = "Product:{product_group_id}"
```

所有 ETL 代码从这个模板读取拼接规则，不允许各自硬编码——否则同一实体可能在不同 ETL 路径下算出不同的 `node_key`，重新引入第 3 节想解决的"身份不稳定"问题。`extraction` 模式的 `term_type` 不需要填这个字段（`node_key` 直接取 `standard_name`，见 3.2）。

---

## 6. `extra_fields` 类型化

现状：`ontology_term_types.extra_fields` 只是字段名白名单（`list[str]`），`Term.extra_properties` 是纯字符串字典（`dict[str, str]`）。MUJI 的 `VariantValue` 需要 `numeric_value`（数字）/`dims`（数组）/`order_rank`（整数）这类真正类型化的字段，才能在 Neo4j 里建索引做数值范围比较（"有 500ml 以上的吗"）——字符串属性无法支持这类查询。

**改造**：

```
extra_fields（改造前）: ["严重等级", "影响范围"]

extra_fields（改造后）: [
  {"name": "严重等级", "value_type": "string"},
  {"name": "numeric_value", "value_type": "number"},
  {"name": "dims", "value_type": "number[]"},
  {"name": "order_rank", "value_type": "integer"},
]
```

`extra_properties` 的校验层（`terms_store.py::_validate_categories`）从"只检查字段名在白名单里"扩展为"检查字段名在白名单里，且值类型匹配声明的 `value_type`"。存储层不需要改动——JSON 本身支持数字/数组，Neo4j 的 `SET t += $extra_properties` 参数化 map 写入也本来就支持非字符串类型，瓶颈完全在校验层。

**明确不做**：MUJI 文档里"同一 `VariantValue` 节点只使用对应 `value_kind` 的结构化字段，其余置空"这条约定不引入 schema 层的条件校验（如"仅当 `value_kind=quantity` 时 `numeric_value` 才允许非空"）——这是应用层/ETL 层的写入约定，不是需要系统强制的完整性约束，过度形式化属于当前没有真实需求支撑的投入。

---

## 7. `schema_etl.py`：ETL 写入引擎

新增模块，职责边界：**读取该租户已 confirm 的 schema（实体类型定义 + 关系类型定义 + domain/range 约束），按列映射配置对源数据做确定性转换，写入 Term/Neo4j 双存储**。不经过 `review_queue.py`（没有幻觉风险，不需要人工审核），仍然同步写入 SQLite 镜像层（`terms_store.py`），保持与 `extraction` 模式一致的双存储架构。

### 7.1 核心能力一：列映射配置驱动的确定性写入

给定租户已 confirm 的 schema + 一份列映射配置（哪些源列对应哪个 `term_type` 的哪些字段、哪些源列对应哪条关系的哪一端），把源表数据转换成 `Term` 记录和关系边，走 `terms_store.py`/`neo4j_client.py` 现有的写入接口落库。列映射配置的具体格式（DSL / 声明式配置 / 代码）留待实施计划阶段设计，本文档只确定它的输入输出边界：**输入是已 confirm 的 schema + 源数据，输出是符合 schema 的 `Term` 写入调用，不允许绕过 schema 层的校验直接拼 Cypher**。

### 7.2 核心能力二：稳定码注册机制

`node_key_template`（第 5.3 节）解决了"已知稳定字段怎么拼成 key"，没解决"这些稳定字段本身第一次是怎么产生的"。MUJI 源表里只有原始值（如"抹茶"），没有现成的 `value_code`——按 MUJI 文档"首次入库分配"的表述，这个分配动作发生在写入图谱这一侧，不是源系统已经提供好的。

因此 `schema_etl.py` 需要一个**通用的、有状态的**稳定码注册机制：

```sql
CREATE TABLE etl_stable_code_registry (
    tenant_id   TEXT NOT NULL,
    scope       TEXT NOT NULL,   -- 稳定码的命名空间，如 "variant_value:颜色"
    raw_value   TEXT NOT NULL,   -- 原始值，如 "抹茶"
    stable_code TEXT NOT NULL,   -- 分配的稳定码，如 "val_00042"
    allocated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, scope, raw_value)
);
```

原始值首次出现时分配新码并持久化；以后重复出现时查表复用同一个码，永不回收、永不重新分配。这是任何 ETL 租户都能复用的通用能力，不是 MUJI 专属——**这意味着 `schema_etl.py` 不是纯无状态的列→图转换，而是需要持久化状态的有状态服务**，实施计划阶段需要把这个特性纳入设计（如并发写入下的分配竞争如何处理）。

### 7.3 明确不做：基数约束

MUJI 关系表里标注的基数（如 `belongs_to_category` 是 N:1）**不进 schema 层**，不新增 cardinality 字段、不做写入时校验。理由：结构化 ETL 的源数据本身结构化程度高（一行 `product_group_id` 天然对应一个 `sel_class`），基数是源数据自身保证的性质，不是图谱层需要重新断言的业务规则；`node_key` 幂等 `MERGE` 天然不会产生重复边。这属于文档对数据现状的描述，不是需要系统强制执行的约束，过早引入属于当前没有真实场景支撑的投入。

---

## 8. 结构化过滤查询工具（Agent 侧，必需任务）

现状 `app/agent/tools.py::graph_query_tool` 只支持"给定标准名 → 查关联子图"的单一模式（底层是 `neo4j_client.py::query_subgraph` 的 1-2 跳遍历），没有任何数值/区间过滤能力。MUJI 文档列为"典型问答走法"的大多数问法（"有 500ml 以上的吗"→`numeric_value>500`、"比 M 码大的有哪些"→`order_rank>2`、"能塞进 80cm 空隙吗"→`dims` 逐边比较）都需要按结构化字段做谓词过滤，现有工具答不了。

这是本次评估确认的**必需任务**，不是可选增强——没有它，即使 schema 建对了、ETL 也写对了，Agent 依然无法回答 MUJI 文档里的核心问法。具体的工具接口设计（新增独立工具 vs 扩展 `graph_query_tool` 支持谓词参数）留待实施计划阶段确定。

---

## 9. 范围之外（不做）

| 不做 | 理由 |
|---|---|
| domain/range 的基数约束（1:N/N:1 等） | 第 7.3 节已论证，源数据结构化程度已保证，不需要图谱层重新断言 |
| `extra_fields` 的条件校验（`value_kind` 决定哪些字段必填） | 第 6 节已论证，属于应用层写入约定，非完整性约束，无真实场景支撑 |
| 通用可视化 schema 建模引擎（脱离 term_type/关系类型的抽象建模 DSL） | 第二个结构化租户业务域虽然完全不同，但仍可通过"各自注册自己的 term_type/关系类型"覆盖，不需要更底层的元建模能力 |
| ETL 触发频率、增量/全量同步策略 | 数据管道的运维细节，不属于 schema 构建架构，留待实施计划阶段单独设计 |
| **数据驱动的 schema 建议工具**（如 MUJI 文档 §3.3 那套"唯一率/非空覆盖率阈值"，自动跑源表统计、建议哪些列该建成实体/维度） | 明确评估过，决定不做。这类统计门槛是一次性接入前期判断，不是持续运行的业务功能——业务一次 SQL 就能算完，为一次性判断建专门工具属于过度工程，与项目一贯 YAGNI 判断一致。本设计的"预先手工定义 schema"（第 5 节）覆盖的是"定义后如何存储/校验/生效"，不覆盖"定义前怎么决策"——后者留给业务自己用外部工具分析源表 |

---

## 10. 待确认事项（留给实施计划阶段）

1. 存量全局 `term_type` 数据（如 `error_code`/`module`）迁移到按租户隔离后的具体归属方案。
2. `schema_etl.py` 列映射配置的具体格式（声明式配置文件 / 数据库表 / 代码）。
3. 稳定码注册表在高并发写入下的分配竞争处理（是否需要行级锁/序列化写入）。
4. 结构化过滤查询工具的具体接口形状（新工具 vs 扩展现有工具）。
5. `docs/ARCHITECTURE.md` 第 9 节多租户设计、第 2 节图谱结构描述（`:Term {name, type, product_line}`）需要同步更新以反映 `node_key`/按租户 `term_type`/两种接入模式的变化——本文档暂不改动该文件，留待实施落地时一并处理。
