# 源端删除的传播

**路线图**：[2026-08-30-foundry-alignment-roadmap.md](2026-08-30-foundry-alignment-roadmap.md)（第四份 spec，可独立交付）

## 背景

Foundry 的对象集合**等于背书数据集的当前内容**。Funnel 重新索引时，数据集里消失的行，对象就从本体里消失——数据源是权威的，本体是它的投影。

我们的 ETL 只有 upsert，**没有任何删除**。`schema_etl.py` 全文没有一处 delete：

- 源 xlsx 里删掉一行 → 那个 Term 永远留在 `terms` 和 Neo4j 里
- 源里不再存在的关系 → 那条边永远留着

数据模型只增不减，无法向源收敛。租户修正了源文件重跑一遍，得到的是新旧并存，而不是修正后的状态。

对照之下，**抽取管道是对的**：`graph_extraction.py:92` 在重新抽取前先 `delete_relations_by_source(source, tenant_id)`，完整的"先删后写"替换语义。ETL 这条路径连这个都没有。

### 一处关键的不对称

| | 能否按源文件圈定 | 原因 |
|---|---|---|
| **关系** | ✅ 能 | `merge_relation(source=mapping.source_file, ...)`，边上带源文件名；`delete_relations_by_source` 已经存在且可用 |
| **实体** | ❌ 不能 | `upsert_term_with_node_key` 的 `source` 取默认值 `"etl"`——那是**渠道**（manual/etl/review/unknown），不是文件名 |

所以今天无法回答"哪些 Term 来自 `soft_drink_sales.xlsx`"。关系侧的机制现成，实体侧要新建。

## 目标

- ETL 重跑后，`terms` 和 Neo4j 只包含源数据当前存在的实体与关系。
- 删除是**可见的**：报告里明确列出本次移除了什么、多少。
- 与人工编辑层（Spec 3）的删除语义有明确、无歧义的优先级。

## 非目标

- 不给 `terms` 加 `source_file` 列（见"设计 A"里为什么不需要）。
- 不做增量/流式的删除检测。ETL 本来就是整份文件重跑的批处理，删除检测跟着这个节奏走。
- 不改抽取管道的删除语义——它已经是对的。

## 设计 A：实体走 mark-and-sweep，按 term_type 圈定

不新增 `source_file` 列，而是利用一个已有的事实：**每个 `term_type` 在配置里恰好对应一个 `EntityMapping`**（`run_schema_etl` 里 `entity_mappings_by_term_type = {m.term_type: m for m in config.entities}` 是按 term_type 建的字典，重复声明会静默折叠）。也就是说，实体侧的"对象类型 ↔ 背书数据源"本来就是 1:1 的——正是 Foundry 要求的那条规则。

于是删除范围可以按 `term_type` 圈定，不需要文件名：

```
对每个 EntityMapping：
  1. projection 算出本次的全部 node_key（Spec 2 已经产出这个集合）
  2. 写入这些实体
  3. sweep：删除该 tenant + 该 term_type 下、node_key 不在本次集合里的行
```

**为什么这样就够**：该 term_type 的全部实体都由这一个映射产出，所以"本次没算出来的"等价于"源里已经不存在的"。

**与 Spec 2 的关系**：sweep 需要"本次的全部 node_key 集合"，而这正是 Spec 2 的 projection 层已经要产出的东西。两份 spec 独立可交付，但一起做时 sweep 几乎是免费的。Spec 2 未落地时，本设计自己在写入循环里收集这个集合即可。

### 人工创建的实体不被 sweep 波及

`terms.source` 记录创建渠道。sweep 只删 `source = 'etl'` 的行——审核界面现场创建的实体（`source = 'review'`）和管理后台手工录入的（`source = 'manual'`）不在扫除范围内，即使它们的 term_type 被 ETL 管理。

这一条是必须的：那些实体从来就不来自这个数据源，"源里没有"对它们不成立。

## 设计 B：关系走已有的按源删除

ETL 在写关系之前，对配置里出现过的每个 `source_file` 调一次 `delete_relations_by_source(source_file, tenant_id=...)`，然后重新写入全部关系。与 `graph_extraction.py:92` 完全同一个模式，复用同一个方法。

注意顺序：**所有源文件的删除必须在所有关系写入之前完成**。配置里多个关系映射可能共享同一个源文件（demo 配置里五条关系全部来自 `soft_drink_sales.xlsx`），逐个映射"先删后写"会让后一个映射的删除抹掉前一个刚写的边。

## 设计 C：与人工编辑层的优先级

Spec 3 落地后，删除有两个来源：源端消失，和人工 `__deleted__`。规则取自 Foundry：

> **存在性由数据源决定；属性由编辑优先；人工删除是唯一能覆盖数据源存在性的编辑。**

| 情形 | 结果 |
|---|---|
| 源里有，无人工删除 | 存在 |
| 源里有，有人工 `__deleted__` | **不存在**——人工删除不可被数据源恢复（Foundry：「Deletions aren't reversible by datasource updates」） |
| 源里没有（被 sweep），无人工编辑 | 不存在 |
| 源里没有（被 sweep），但有人工属性编辑 | 不存在，但**编辑保留**——源里若再出现同 `node_key`，编辑重新生效（Foundry：「When a datasource row reappears after deletion, previous edits remain applied」） |

最后一行意味着 **sweep 只删 `terms` 行，不删 `term_edits` 行**。这与 Spec 3 的字段级编辑模型天然兼容：编辑挂在 `node_key` 上，实体在不在不影响编辑的存续。

Spec 3 未落地时，这一节不适用；本设计的其余部分独立成立。

## 设计 D：删除必须可见，且有安全阀

### 报告

`ETLRunReport` 新增：

```python
entities_removed: int
entities_removed_by_type: dict[str, int]
relations_removed: int
```

报告里逐类型列出移除数量。**删除数量为零时也要出现在报告里**——"本次没有移除任何实体"和"没跑删除逻辑"必须能区分开。

### 安全阀

sweep 会删除的行数超过该 term_type 现有行数的 **50%** 时，整轮失败、零改动，报错说明：

```
实体类型 '订单号' 的清理将移除 8000 / 10000 行（80%），超过安全阈值，本次未做任何改动。
如果源文件确实缩减到这个规模，用 --allow-large-sweep 重跑。
```

理由与 Spec 2 的"主键重复整体失败"同源：一次误传的、被截断的源文件会静默清空大半个图谱，而症状要等用户提问答不出来才暴露。阈值和绕过开关让"我确实要缩减"这件事必须被显式表达。

**阈值是启发式，不是正确性保证**——它拦不住 49% 的误删。它的作用是把最常见的事故形态（传错文件、导出被截断）挡在门外。

## 迁移

无 schema 变更。首次带 sweep 的运行会清理掉历史累积的孤儿实体和边——**这可能是一次规模不小的删除**，而且安全阀很可能会触发。

建议实施任务提供一个 dry-run 模式（只报告将要删除什么、不实际删除），让租户在首次启用前先看一眼。

## 测试策略

- **源端删除传播**：写入 3 行 → 源文件删掉 1 行 → 重跑 → 断言 `terms` 只剩 2 行，Neo4j 对应节点也删了。
- **人工创建不被波及**：ETL 写入若干实体 + 审核界面创建一个同类型实体（`source='review'`）→ 重跑 ETL → 断言人工那条仍在。
- **关系全量替换**：源里删掉一条关系 → 重跑 → 断言该边消失，其余边仍在。
- **多映射共享源文件**：五条关系映射同源，重跑后五种关系都在（钉住"删除必须先于所有写入"这条顺序）。
- **安全阀**：源文件缩减到 20% → 断言整轮失败且**零改动**（`terms` 行数不变、边数不变）。
- **与编辑层的优先级**（Spec 3 落地后）：源端删除后 `term_edits` 仍在；源里恢复同 node_key 后编辑重新生效。

## 未决风险

- **删除窗口。** 关系走"先全删再全写"，中途失败会留下一个边被删光、实体还在的图谱。抽取管道有同样的性质（`graph_extraction.py:92`），所以不是新引入的风险，但 ETL 的数据量更大、窗口更长。是否需要事务化（或先写新边再删旧边）留给实施任务评估——SQLite 侧可以用事务，Neo4j 侧跨语句没有事务保证。
- **孤儿边。** sweep 删除实体时，Neo4j 上指向它的边如果不一并清理，会留下悬空引用。`delete_term_node` 的行为需要确认（是否 DETACH DELETE），实施任务必须覆盖"删实体时它的边也没了"这条断言。
- **安全阀阈值没有依据。** 50% 是拍的。真实租户的数据波动幅度未知，可能过松也可能过紧。实施任务应让它可配置，并在若干次真实运行后回头调整。
- **`terms.source` 承担了新职责。** 它原本只是"创建渠道"的可观测性字段，现在成了 sweep 的过滤条件——语义从"记录"升级成"控制"。如果将来有实体先由 ETL 创建、后被人工编辑，`source` 仍是 `'etl'`（`update_term` 不改 source），它会被 sweep 删掉，而人工编辑保留在 `term_edits` 里。这个行为是符合设计 C 的（存在性由数据源决定），但依赖 `source` 不被改写这个既有约定，实施任务应当把它钉进测试。
- **本设计不处理"源文件本身消失"。** 租户删掉整个上传文件、或从配置里移除某个 EntityMapping 时，该 term_type 的实体不会被清理——sweep 只在该映射真的跑了的时候才发生。这属于配置生命周期管理，不在本次范围内。

## Global Constraints

- sweep 只删除 `source = 'etl'` 的实体行；`manual`/`review`/`unknown` 永不被 ETL 清理。
- 关系的删除必须在所有关系写入**之前**全部完成，不能逐映射先删后写。
- sweep 只删 `terms` 行，不删 `term_edits` 行（Spec 3 落地后）。
- 删除数量必须出现在运行报告里，零删除时也要出现。
- 安全阀触发时整轮零改动，不做部分清理。
