# 向 Foundry 模型对齐：路线图

**这不是一份实现 spec。** 它承载四份可独立交付的 spec 共用的东西：Foundry 的调研结论、现状对照、以及四份 spec 之间的依赖与顺序。每份 spec 各自完整，不需要读完这份才能读它们；这份只回答"为什么是这四份、为什么是这个顺序"。

## 背景

2026-08-30 重建 demo 租户时，`用户名` 的复合节点键撞上 `standard_name` 唯一索引，10000 行只写出 9335 个客户实体，665 笔订单因此失去客户边。追查下去，根因不是一个索引定义写错了，而是三处结构性问题：

1. **身份与展示名在存储层耦合**——ADR-0003 已经把 `node_key` 和 `standard_name` 分开，但唯一索引仍然压在 `standard_name` 上。
2. **键在写入时才计算，从未以数据的形态存在过**——`compute_node_key` 在 `_write_entity_mapping` 的行循环里现算，冲突只能表现为"写到第 N 行开始报错"，无法在写入前检查。
3. **人工编辑与管道产出混在同一张表里**——`terms` 表用 `source` 列区分来源，但物理上是同一批行。ETL 重跑时人工修正会不会被覆盖、覆盖了有没有记录，都没有明确答案。

这三处恰好对应 Palantir Foundry 从架构上避开的三件事。

## Foundry 的关键设计（调研结论）

**分层的数据管道。** 推荐的项目结构是四层：Data Connection（原始接入）→ Datasource（基础清洗、统一 schema）→ Transform（可复用的规范化数据集）→ Ontology（每个对象类型一张背书表）。文档明确要求「Cleaning and formatting should be done upstream in data transformations, not the Ontology」，并且「An intermediate transform step from clean to ontology is always recommended」——理由是 clean 层的列比本体需要的多，分开才能给本体背书表加派生列（比如算出来的主键）而不污染 clean 层。

**主键是背书数据集上的一列，本体不计算它。** 「The property that acts as a unique identifier for each instance of an object type. Each row in the backing datasource must have a different value for this property.」复合键的官方解法是在管道里拼出来：「define pipeline logic such that the primary key is the function of either a single column or multiple columns」。主键必须确定性——「If the primary key is non-deterministic and changes on build, edits can be lost and links may disappear.」

**Title 与主键分离，且明确不要求唯一。** 文档举的例子就是多个员工可以同名。

**人工编辑走独立的 writeback 层。** 「edits are written to the writeback dataset and not the dataset backing an object type or link type. This ensures that users have access to both the original data and the edited data in their analyses.」Funnel 服务把「数据源 + 用户编辑」合并成 merged dataset，合并策略有两种：默认 *Apply User Edits*（人工编辑对被编辑的属性永远优先，未编辑的属性正常接受数据源更新），或 *Apply Most Recent Value*（按时间戳比较，需要背书表带时间戳列）。删除不可被数据源恢复；对象被重建时旧编辑不会带过来。

**编辑通过 Action Type 发生，不是直接改表。** Action 是「a single transaction that changes the properties of one or more objects」，带参数、提交校验、业务规则和副作用，且是权限感知的。

**对象类型与背书数据集一对一。** 「Object types are backed by a single dataset, and a dataset can back only one object type.」

**本体定义本身走分支 + 提案。** Ontology proposal「analogous to a Pull Request」，在分支上改、评审通过后合并进 main。

**主键重复是硬错误。** Object Storage v2 直接让 build 失败；旧的 v1 会「appear as successful; however, the duplicate primary keys can cause unexpected changes」——静默改坏数据。Foundry 自己从静默演进到了失败。

## 现状与目标对照

| Foundry | 本项目现状 | 目标 | 归属 |
|---|---|---|---|
| Primary key（背书表上的一列） | `node_key`，写入时现算 | projection 层物化成列 | Spec 2 |
| 重复主键 → build 失败 | 逐行 `TermNameConflictError` 跳过 | 写入前预检、整体失败 | Spec 2 |
| Datasource 层（清洗、统一 schema） | 无，xlsx 直接进 ETL | staging 层 | Spec 2 |
| Title（不唯一） | `standard_name`，**唯一索引** | 取消唯一索引 | Spec 1 |
| Title 与主键分离 | 展示名只能取单列 | 展示名支持多列拼接 | Spec 1 |
| 对象按主键寻址 | 管理后台按 `standard_name` 寻址 | 改按 `node_key` | Spec 1 |
| Writeback dataset（编辑独立存储） | 无，人工编辑直接改 `terms` 行 | `term_edits` 表 | Spec 3 |
| Merged dataset（数据源 + 编辑） | 无 | 合并视图，读路径统一走它 | Spec 3 |
| Action Type（结构化编辑事务） | 直接 PUT/DELETE | 编辑写成带出处的事件 | Spec 3（只取内核） |
| 对象集合 = 背书数据集当前内容 | ETL 只 upsert，**从不删除** | 源端删除传播到本体 | Spec 4 |
| 对象类型 ↔ 背书数据集 1:1 | 一个文件背书五个 term_type | projection 每类型一份产出 | Spec 2 |
| 链接类型声明基数 | 无基数声明，运行时探测扇出 | **不改**，见下 | — |
| 本体定义走分支/提案 | draft/confirm 两阶段 | **不改**，见下 | — |

## 四份 spec 与顺序

### Spec 1 · 标准名唯一性下沉到 node_key
`2026-08-30-name-uniqueness-to-node-key-design.md`

取消 `standard_name` 唯一索引，展示名支持多列拼接，管理后台改按 `node_key` 寻址，并让消歧失败可区分。

**独立性**：不依赖另外三份。**这是唯一能直接修掉线上缺陷的一份**——落地后 demo 租户的 `用户名` 可以用复合键而不丢边。

**为什么排第一**：它修的是已经在流血的伤口，另外三份是防止再次流血、或让伤口可见。

### Spec 2 · ETL 分层管道与写入前校验
`2026-08-30-etl-layered-pipeline-design.md`

把 ETL 拆成 staging（解析归一）→ projection（物化 `node_key` 与展示名）→ 写入三层，并在写入前做全量主键查重，冲突则整体失败、零写入。

**独立性**：不依赖 Spec 1、3、4。即使不做 Spec 1，分层本身也成立。

**为什么排第二**：它改变的是**故障如何暴露**，不是故障是否存在。Spec 1 落地后重复键在合法场景下不再产生，这一份是给未来的配置错误兜底。

### Spec 3 · 人工编辑独立成层
`2026-08-30-manual-edits-layer-design.md`

新增 `term_edits` 表承载人工编辑，读路径走「管道产出 + 编辑」的合并视图。ETL 只写 `terms`，人工只写 `term_edits`。

**独立性**：机制上不依赖前两份。

**为什么排第三**：改动面最大（所有读路径都要改走合并视图），且它解决的问题今天还没造成过事故。排在 Spec 4 之前，是因为 Spec 4 的删除语义要先知道人工编辑有没有独立层，才定得清楚。

### Spec 4 · 源端删除的传播
`2026-08-30-source-deletion-propagation-design.md`

ETL 重跑时清理源里已消失的实体与关系，让本体向数据源收敛。实体按 `term_type` 做 mark-and-sweep（只扫 `source='etl'` 的行），关系复用已有的 `delete_relations_by_source`，并配一个防误删的安全阀。

**独立性**：不依赖前三份。与 Spec 2 一起做时更省——sweep 需要的"本次全部 node_key 集合"正是 projection 的产物；与 Spec 3 一起做时需要定义存在性优先级，该 spec 已经给出。

**为什么排最后**：它是四份里唯一会**删除数据**的，风险最高，适合在前三份把模型理顺之后再动。

### 依赖关系

```
Spec 1（身份/展示名解耦）   ── 独立 ──▶ 修掉当前缺陷
Spec 2（分层管道 + 预检）   ── 独立 ──▶ 改变故障暴露方式
Spec 3（编辑独立成层）      ── 独立 ──▶ 改动面最大
Spec 4（源端删除传播）      ── 独立 ──▶ 唯一会删数据的一份

四者无强依赖，但建议按 1 → 2 → 3 → 4 实施：
  1 修掉现有缺陷
  2 让同类缺陷提前暴露
  3 让重跑不再伤害人工修正
  4 让本体能向数据源收敛
```

四份都不共享改动文件，可以并行开发；顺序建议只出于价值与风险排序，不是技术约束。

Spec 2 与 Spec 4 有一处**协同而非依赖**的关系：Spec 4 的 sweep 需要"本次算出的全部 node_key"，Spec 2 的 projection 恰好产出它。两者独立可交付，一起做时 sweep 几乎免费。

## 明确不做的事，以及理由

**不建通用数据平台。** Foundry 的 transform 层是完整的数据平台（任意变换、血缘、构建编排）。Spec 2 只做本体背书这一条链路需要的最小分层，不做通用变换。

**不做 Action Type 的完整形态。** Spec 3 只取「编辑是带出处的独立事件、不直接改背书数据」这一条内核，不做参数化表单、提交校验、业务规则和副作用。如果后续需要"审核员只能改这几个字段""改动需要二次批准"，届时再在编辑层之上加一层。

**不做本体定义的分支/提案。** 现有的 draft/confirm 两阶段生命周期（`ontology_lifecycle.py`）覆盖了同一需求的实用子集：草稿可编辑、确认后原子提升。替换成 Foundry 的分支模型收益不足以匹配改造成本。如果将来出现多人并行改同一租户本体的场景，这个判断需要重新评估。

**不给链接类型加基数声明。** Foundry 在链接类型上声明基数（一对多/多对多），多对多还必须有自己的背书数据源。我们的 `AllowedCombination` 只有 `(subject, relation, object)`，没有基数——2026-08-29 的扇出陷阱检测因此选择了**运行时探测**（`probe_relation_fanout`）而不是读取声明。当时的判断是：人工录入的基数会静默失效（填错了没人发现，而错误的基数比没有基数更危险），而运行时探测拿到的是图上的事实。这个判断仍然成立，本轮不推翻。代价是每次多跳计数都要多一次探测查询。

**不改抽取管道的结构。** Foundry 没有对应物——它的对象来自数据集，不来自"LLM 猜 + 人审核"。我们的链路（`graph_extraction.py` → `normalization.py` → `review_queue.py`）在 Foundry 的语汇里可以理解为「LLM 抽取是一个产出候选关系的 transform，人工审核是一个 Action」，但结构本身是对的，不需要改。Spec 3 只调整其中一处：审核界面现场创建实体（`GraphReviewsPage.tsx:525`）改为写编辑层。

## 与 Foundry 的根本差异（四份 spec 都受它约束）

Foundry 的对象只来自数据集，编辑只能修改**已存在**对象的属性。

我们不同：审核员在批准一条 LLM 抽取的关系时，可能需要**当场创建**一个尚不存在的端点实体（`GraphReviewsPage.tsx:525`，`source: 'review'`）。这个路径是抽取管道能闭环的必要条件，删不掉——ADR-0002 定下的方向就是两种接入模式共享同一套 schema 和数据层，而抽取模式天然需要人在环内创作。

这条差异还有第二个后果，落在 Spec 1 上：**Foundry 的 Title 对任何来源都不唯一，没有按路径分化**；而 Spec 1 选择"人工录入路径保留重名检查、ETL 路径去掉"。这是因为我们有 Foundry 没有的人工创作路径——人手工敲进一个已存在的名字，绝大多数是笔误而非有意。这个分化是对自身架构的响应，不是对 Foundry 的偏离性妥协，但读者对照时会发现它，所以在此点明。

这意味着 Spec 3 的编辑层要承担一件 Foundry 编辑层不承担的事：创建全新实体。由此引出一个无先例可循的问题——**纯由编辑层创建、`terms` 表里没有对应行的实体，如果 ETL 后来产出了同 `node_key` 的行，合并语义是什么？** Spec 3 必须回答它。

## 参考

- [Ontology overview](https://www.palantir.com/docs/foundry/ontology/overview) — 语义层与动力层
- [Create an object type](https://www.palantir.com/docs/foundry/object-link-types/create-object-type) — 主键与 Title 的规则
- [How user edits are applied](https://www.palantir.com/docs/foundry/object-edits/how-edits-applied) — 合并策略与删除语义
- [Action types overview](https://www.palantir.com/docs/foundry/action-types/overview) — 编辑作为事务
- [Object backend overview](https://www.palantir.com/docs/foundry/object-backend/overview) — Funnel 与 Object Storage v2
- [Recommended project structure](https://www.palantir.com/docs/foundry/building-pipelines/recommended-project-structure) — 四层管道结构
- [Ontology proposals](https://www.palantir.com/docs/foundry/ontologies/ontologies-proposals) — 本体定义的 PR 式评审
