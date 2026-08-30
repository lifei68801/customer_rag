# 人工编辑独立成层

**路线图**：[2026-08-30-foundry-alignment-roadmap.md](2026-08-30-foundry-alignment-roadmap.md)（四份 spec 中的第三份，可独立交付）

## 背景

`terms` 表同时接受两条写入：结构化 ETL 和人工编辑。`source` 列（`manual`/`etl`/`review`/`unknown`）标记了行的来源，但物理上是同一批行——人工改过的字段和 ETL 写入的字段挤在同一行里，无法区分。

于是几个问题今天没有答案：

- ETL 重跑时，人工改过的 `standard_name` 会不会被数据源的值盖掉？（会——`upsert_term_with_node_key` 的 `ON CONFLICT DO UPDATE SET standard_name = excluded.standard_name`）
- 人工删掉的实体，ETL 重跑会不会让它复活？（会——`upsert` 会重新插入）
- 哪些字段是人工改过的？（无从得知）

这不是假想的风险。`用户名` 这类实体的展示名、别名，以及 `duplicate_review_queue` 批准合并后写入的别名，都是人工产物；任何一次 ETL 重跑都会静默抹掉它们。

Foundry 从架构上避开了这件事：「edits are written to the writeback dataset and not the dataset backing an object type or link type. This ensures that users have access to both the original data and the edited data in their analyses.」Funnel 把「数据源 + 用户编辑」合并成 merged dataset 供查询，两者物理分离。

## 目标

- 人工编辑写入独立的 `term_edits` 表，不与管道产出混在同一批行里。
- 读路径统一走「管道产出 + 编辑」的合并视图。
- ETL 重跑对人工修正完全无害。
- 人工删除不可被 ETL 恢复。

## 非目标

- 不做 Action Type 的完整形态（参数化表单、提交校验、业务规则、副作用、权限矩阵）。只取「编辑是带出处的独立事件、不直接改背书数据」这一条内核。
- 不做编辑历史/审计流水。`term_edits` 保存的是**当前编辑状态**，每个 `(node_key, field)` 一行，不是 append-only 日志。需要审计时再单独设计。
- 不改抽取管道的结构（`graph_extraction.py` → `normalization.py` → `review_queue.py`）。只调整其中一处写入点。
- 不引入 Foundry 的 *Apply Most Recent Value* 合并策略（见"合并语义"）。

## 数据模型

```sql
CREATE TABLE IF NOT EXISTS term_edits (
    tenant_id     TEXT NOT NULL,
    node_key      TEXT NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,
    edited_at     TEXT NOT NULL,
    edited_by     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, node_key, field)
);
```

`field` 的取值：

| 取值 | 含义 | `value` |
|---|---|---|
| `standard_name` | 改展示名 | JSON 字符串 |
| `aliases` | 改别名列表 | JSON 数组 |
| `extra_properties.<name>` | 改某个属性字段 | JSON 标量 |
| `term_type` | 改实体类型 | JSON 字符串 |
| `__deleted__` | 删除该实体 | `null` |
| `__created__` | 该实体完全由编辑层创建 | JSON 对象，含创建时的完整字段 |

**字段级而不是整行级。** 这是合并语义的前提，也是 Foundry 的做法：「Unedited properties receive datasource updates normally」。整行覆盖会让 ETL 对未编辑字段的更新一并失效——比如人工只改了展示名，却导致该实体的金额再也不跟着数据源更新。

## 合并语义：Apply User Edits

采用 Foundry 的默认策略：**人工编辑对被编辑的字段永远优先，未被编辑的字段正常接受 ETL 更新**。

不采用 *Apply Most Recent Value*（按时间戳比较）：它要求背书数据带时间戳列，而我们的源文件是客户上传的 xlsx，不保证有这一列；强行要求等于把负担推给租户。

### 删除

`__deleted__` 编辑不可被 ETL 恢复，照搬 Foundry 的「Deletions aren't reversible by datasource updates」。这一条必须显式实现——否则重跑 ETL 会让人工删掉的实体复活，而这正是今天的行为。

合并视图遇到 `__deleted__` 时把该实体整个排除，`terms` 表里对应的行仍然存在（ETL 还在维护它），只是对所有读路径不可见。

### 编辑层创建的实体

这是 **Foundry 没有先例的一块**，路线图里已经标出：Foundry 的编辑只能修改已存在对象的属性，而我们的审核员在批准一条 LLM 抽取的关系时，可能需要当场创建一个尚不存在的端点实体（`GraphReviewsPage.tsx:525`，`source: 'review'`）。这个路径是抽取管道能闭环的必要条件，删不掉。

于是必须回答：**纯由编辑层创建（`terms` 表无对应行）的实体，如果 ETL 后来产出了同 `node_key` 的行，合并语义是什么？**

**本设计的答案：ETL 的行接管该实体的存在性，`__created__` 里记录的字段降级为普通字段级编辑。**

理由：`__created__` 的语义是"这个实体在数据源里不存在，我先建一个"。一旦数据源真的产出了它，那个前提就不再成立——数据源是更权威的来源。但当初手工填的那些字段值仍然是人的判断，应当继续按字段级编辑优先。

具体行为：`__created__` 的每个字段等价于一条同字段的普通编辑；ETL 产出该 `node_key` 后，未被 `__created__` 覆盖的字段正常取 ETL 值。

**这条语义没有外部先例可循，是本设计自己的判断，必须用测试钉死。**

## 写路径分化

| 路径 | 写哪张表 |
|---|---|
| `schema_etl.py::_write_entity_mapping`（ETL） | 只写 `terms` |
| `admin_terms_routes.py` 的 PUT / DELETE（管理后台编辑） | 只写 `term_edits` |
| `GraphReviewsPage` 审核界面现场创建实体 | 只写 `term_edits`（`__created__`） |
| `duplicate_review_queue::approve_duplicate_suggestion`（合并术语） | 只写 `term_edits` |

**这条分化是本设计的全部价值所在。** 任何一处违反——比如某个人工路径直接改了 `terms`——都会让"重跑 ETL 不伤人工修正"的保证失效，且失效是静默的。Global Constraints 里再强调一次。

## 读路径统一走合并视图

新增 `list_terms_merged` / `get_term_merged`，在 `terms` 之上叠加 `term_edits`。**所有读路径改为走它**：

- `resolve_term` 的术语列表来源（`agent_routes.py`/`qa_routes.py`/`voice_routes.py` 等处的 `list_terms` 调用）
- 管理后台的实体列表（`admin_terms_routes.py::list_all_terms`）
- `structured_filter_query` 的 `terms` 入参
- `duplicate_detection_worker` 的候选来源
- Neo4j 同步（见下）

### Neo4j 侧

图谱是**合并结果**的投影，不是 `terms` 的投影：

- `sync_term` 的入参从合并视图取。
- `__deleted__` 的实体不同步，并在图上删除对应节点。
- ETL 写入实体后触发的同步，也要走合并视图——否则 ETL 刚写完的原始值会盖掉图上的人工修正。

## 迁移

- `term_edits` 新建空表，加进 `ontology_store.py::open_ontology_store_conn` 的建表清单（该 module 是 2026-08-30 建立的唯一建表入口）。
- **存量人工编辑无法回填。** `terms` 表没有记录哪些字段是人工改的——`source` 列只标记行的**创建**渠道，不标记后续编辑。存量行一律视为管道产出。这意味着本设计上线前已经存在的人工修正，在下一次 ETL 重跑时仍然会被覆盖一次；之后的修正才受保护。这个一次性损失需要在上线前告知租户，或在上线前先跑一次 ETL 让两侧对齐。

## 测试策略

- **ETL 重跑不伤编辑**：写入实体 → 人工改展示名 → 重跑 ETL → 断言展示名仍是人工值，而未编辑的属性字段取到了 ETL 的新值。这条是本设计的核心保证。
- **删除不可恢复**：人工删除 → 重跑 ETL → 断言该实体在合并视图和图谱里都不出现。
- **字段级隔离**：只编辑 `standard_name`，断言 `extra_properties` 仍随 ETL 更新（防止退化成整行覆盖）。
- **编辑层创建 + ETL 后续产出**：`__created__` 一个实体 → ETL 产出同 `node_key` → 断言 `__created__` 覆盖过的字段保持人工值，未覆盖的字段取 ETL 值。这条钉的是上面那个无先例的判断。
- **写路径分化**：断言 ETL 路径不产生任何 `term_edits` 行；断言管理后台的 PUT 不修改 `terms` 表。
- **Neo4j 同步走合并视图**：人工改展示名后，图上节点的 `standard_name` 是人工值。

## 未决风险

- **改动面是四份 spec 里最大的。** 所有读路径都要改走合并视图，漏掉一处就会读到未合并的原始数据——而且是静默的（读到的是合法的旧值，不报错）。实施任务需要一次穷举：`grep list_terms` 的全部调用点逐个确认。
- **合并视图的性能。** 现状 `list_terms` 是一次全表查询；合并视图要再查 `term_edits` 并在 Python 里叠加。`term_edits` 通常远小于 `terms`（只有被人工碰过的行），但 `list_terms` 在部分路径上是**每请求**调用的（Agent 每轮对话都要拿术语表做消歧）。实施时需要测量，必要时加缓存——但缓存会引入"人工改完多久生效"的新问题，不能默认加。
- **`duplicate_review_queue` 的合并操作要重新表达。** 它今天通过 `merge_terms` 直接改 `terms`（墓碑化 + 别名追加，两步 `update_term`）。改成写编辑层之后，"墓碑"这个概念要重新映射——最直接的对应是 `__deleted__` 加上 keep 那条的 `aliases` 编辑。这一处的改造复杂度可能不低于本设计的其余部分，实施时可以考虑单独成任务。
- **`__created__` 与 ETL 产出相遇的语义没有外部先例。** 见"编辑层创建的实体"。
- **不做编辑历史。** `term_edits` 每个 `(node_key, field)` 只保留当前值，改两次只剩最后一次。如果将来需要"谁在什么时候改成了什么"的审计，需要改成 append-only 并加一层当前值物化。

## Global Constraints

- ETL 写入路径**永不**写 `term_edits`；人工编辑路径**永不**写 `terms`。这条是本设计的全部价值所在。
- 合并策略固定为 Apply User Edits（编辑对被编辑字段优先），不引入时间戳比较。
- 编辑是**字段级**的，不是整行级。
- 人工删除不可被 ETL 恢复。
- Neo4j 是合并结果的投影，不是 `terms` 的投影。
- `node_key` 是编辑与管道产出之间唯一的对应键；本设计不引入任何会改写 `node_key` 的路径（ADR-0003）。
