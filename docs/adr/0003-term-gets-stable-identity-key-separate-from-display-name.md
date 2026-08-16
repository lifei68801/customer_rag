# Term 增加独立稳定身份键，standard_name 降级为展示属性

**Status**: proposed

Term 当前的身份键就是 `standard_name`：SQLite 主键（`terms_store.py`）、Neo4j `MERGE` 匹配键（`neo4j_client.py` 的 `MERGE (t:Term {standard_name: $standard_name})`）都直接用这个人工可读的显示名字段。改名要走专门的 `rename_term_node` 接口（`MATCH` 旧名 → `SET` 新名），这是为人工在后台点一次"改名"按钮设计的操作。

MUJI 的 ETL 场景需要重复、增量地把源表数据同步进图谱（几十万行 SKU，源表的 `label_cn`/显示文本随时可能变）。如果身份键就是显示名，ETL 每次重跑都要自己判断"这行数据是新建还是改名"，否则同一个实体会因为显示名变化而在图里产生两个节点。MUJI v6 文档把这个问题当成头等设计目标解决——`value_code`/`product_group_id`/`jan` 等身份码永远不变，显示名（`label_cn`）是普通属性，ETL 只需按身份码做幂等 `MERGE`，不需要任何"是不是改名"的判断逻辑。这正是 v6 相对 v5 的第一条改动理由（"原方案把显示值拼进主键，改名会断边"）。

评估 MUJI 接入时发现，如果继续复用 Term 现有的"身份键=显示名"设计，等于把 MUJI 已经解决过的问题重新引入一遍。因此改为：Term 增加一个独立的稳定身份键字段（`node_key`），作为 SQLite 主键和 Neo4j `MERGE` 的真正依据；`standard_name` 降级为普通展示属性，可以自由修改而不触发任何"改名"特殊处理。

## Considered Options

- ETL 层自己维护"稳定码 → 显示名"的映射，不动 Term 模型：改动范围小，但把本该属于数据模型的身份语义泄漏到 ETL 层，且 LLM 抽取场景以后如果也想要同样的"改名不特殊处理"能力，无法直接受益。
- 两套身份识别机制并存（LLM 抽取用 standard_name，ETL 用外挂稳定键）：避免动已上线代码，但两套语义长期并存会成为认知负担，且违背刚确认的"两种接入模式共享同一套 schema/数据层"的方向（见 ADR-0002）。
- **（选定）Term 核心模型增加 node_key，statically 与 standard_name 分离**：一次性把"稳定身份"这个概念做对，两种接入模式都受益。LLM 抽取场景里没有外部系统分配稳定码，`node_key` 在创建时可直接取当时的 `standard_name` 值（自动生成、创建后不变），行为对现有用户几乎无感——今天的"改名"操作，效果上变成只更新 `standard_name` 这个展示属性，不再需要判断/重建 Neo4j 匹配键。

## Consequences

- `terms_store.py` 的 `terms` 表主键从 `standard_name` 改为新的 `node_key`；`standard_name` 需要单独的唯一性约束（业务上仍要求"同一 standard_name 不能被两个术语占用"，但不再是物理主键）。
- `neo4j_client.py` 的 `MERGE (t:Term {standard_name: ...})` 改为 `MERGE (t:Term {node_key: ...}) SET t.standard_name = ...`；`sync_term`/`merge_relation`/`query_subgraph` 等所有按 `standard_name` 匹配节点的 Cypher 都要改成按 `node_key` 匹配。
- `rename_term_node` 的语义变化：从"改变节点的匹配键"变成"更新一个普通展示属性"，理论上比现在的 `MATCH` 旧名`→SET` 新名模式更安全（不再要求调用方知道当前的准确显示名才能定位节点）。
- 这是对已经上线、被多处测试覆盖的 Term/terms_store/neo4j_client 核心数据模型的真实改动，需要走一次完整的 schema 迁移（存量 `terms` 表数据要为每行回填 `node_key`，LLM 抽取场景下可直接拿当时的 `standard_name` 值回填）和全套回归测试，不是一个孤立的新增字段。

## 追加：node_key 拼接规则记在 schema 层

`node_key` 稳定只解决了"同一个键不会变"，没解决"同一个实体每次都算出同一个键"——如果拼接规则（用哪些字段、按什么模板，如 `Variant:{dim_code}:{value_code}`）只活在某次 ETL 代码里，换一个人改代码、或同一实体被第二条 ETL 管道处理，就可能拼出不同的 `node_key`，同一实体在图里裂成两个节点，等于把 ADR-0003 想解决的问题换个形式重新引入。

因此 `ontology_term_types`（`ontology_categories.py`）在 `extra_fields` 之外新增 `node_key_template` 字段，跟随 term_type 定义一起在 schema 层声明（例："Variant:{dim_code}:{value_code}"、"Product:{product_group_id}"）。所有 ETL 代码从这个模板读取拼接规则，不允许各自硬编码。这让 schema 层成为"这个实体类型的身份怎么算"的唯一真相源，而不只是"这个实体类型有哪些字段"的真相源。
