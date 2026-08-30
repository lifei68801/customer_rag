# 标准名唯一性下沉到 node_key

**路线图**：[2026-08-30-foundry-alignment-roadmap.md](2026-08-30-foundry-alignment-roadmap.md)（四份 spec 中的第一份，可独立交付；**唯一直接修掉线上缺陷的一份**）

## 背景

ADR-0003 把 Term 的身份键和展示名分开了：`node_key` 是身份（创建后不变），`standard_name` 降级为普通展示属性。但降级只做了一半——`terms` 表上仍然有 `idx_terms_tenant_standard_name`（`terms_store.py:29`），约束 `(tenant_id, term_type, standard_name)` 唯一。身份和标签在存储层依然耦合。

2026-08-30 重建 demo 租户时这个耦合造成了真实的数据丢失。源数据（`soft_drink_sales.xlsx`，10000 行销售流水）里有 530 个姓名被多行共用，但它们的邮编各不相同——是不同的人，不是同一个客户下了多单。为区分他们，`用户名` 的 ETL 配置改成了复合节点键 `[Customer Name, Customer Zip Code]`：

```
第 N 行  William Jackson + 72848  ->  node_key = 用户名:William Jackson:72848
第 M 行  William Jackson + 68046  ->  node_key = 用户名:William Jackson:68046
```

两个 node_key 不同，身份层面已经分开了。但 `standard_name_column` 只能指一列（`schema_etl_config.py::EntityMapping.standard_name_column` 是 `str`），两行的 `standard_name` 都是 `William Jackson`，第二行撞上唯一索引，被 `upsert_term_with_node_key` 抛 `TermNameConflictError` 跳过。

结果是 10000 行只写出 **9335** 个客户（正好等于不同姓名数），**665 行的客户实体没有落库**。随后 `ORDER_BY` 关系写入时，这 665 行算出的端点 node_key 在术语表里不存在，被 2026-08-29 新增的端点存在性守卫（`schema_etl.py::_write_relation_mapping`）逐行跳过——`订单号 -> 用户名` 只有 9335 条边，665 笔订单没有客户。

改动前（姓名单键）不丢边，因为两行算出同一个 node_key，走 upsert 的 update 分支，`_check_name_conflict` 的 `exclude_node_key` 会把"自己"排除掉。代价是 665 行的客户被错误合并进同名者名下。

所以当前两种配置都是错的，只是错法不同：

| 配置 | 客户节点 | ORDER_BY 边 | 缺陷 |
|---|---|---|---|
| 姓名单键 | 9335 | 10000 | 665 行的客户被静默合并成别人 |
| 姓名+邮编（当前） | 9335 | 9335 | 665 笔订单静默失去客户边 |
| 本设计要达到的 | 10000 | 10000 | — |

## 目标

- `standard_name` 不再承担唯一性，身份完全交给 `node_key`。
- ETL 的展示名支持多列拼接，同名不同实体在管理界面上可区分。
- 名字解析遇到多候选时，失败必须是**可区分的**，不能和"没找到"压成同一个结果。

## 非目标

- 不改 `node_key` 的生成规则（`compute_node_key` 及 `node_key_parts` 配置形状不变）。
- 不做自动的实体消歧/合并推断——同名是否为同一实体，本设计只负责"能表达两者不同"，不负责"自动判断是否相同"。那是 `duplicate_review_queue` 那条线的事。
- 不改 Neo4j 侧的节点身份——`sync_term`/`merge_relation` 早已按 `node_key` MERGE，不受影响。

## 设计 A：删除唯一索引，唯一性交给 node_key

`terms` 表的主键本来就是 `(tenant_id, node_key)`，身份约束已经在那里。删掉 `idx_terms_tenant_standard_name`，改建一个同列的**非唯一**索引（`standard_name` 仍是 `resolve_term`/`get_term` 的高频查询列，索引本身要留）。

### 迁移

`terms_store.py` 现有三个迁移函数（`:61-152`），本次追加第四个，形状与它们一致：`PRAGMA index_info('idx_terms_tenant_standard_name')` 探测当前索引是否为 UNIQUE，是则 DROP 后重建为普通索引。幂等，重复调用无副作用。

已有数据不需要动：删除唯一约束只会让原本被拒绝的写入变成可能，不影响任何已存在的行。

### 写入策略：数据库不再强制，人工录入路径仍然强制

这是本设计的关键取舍。写入 Term 的生产路径只有两条：

- `create_term`/`update_term`（`admin_terms_routes.py:149`、`:204`）——人工在管理后台录入
- `upsert_term_with_node_key`（`schema_etl.py:254`）——ETL 按配置确定性写入

**人工路径保留 `_check_name_conflict`**：一个人手工敲进一个已存在的名字，绝大多数情况是笔误而不是"我确实要建一个同名的不同实体"。数据库不再兜底之后，这层策略性检查反而更重要。

**ETL 路径去掉这个检查**：这正是本设计要解开的那个结。ETL 的身份判据是配置里声明的 `node_key_parts`，标准名重不重复不是它该关心的事。

于是"名字能不能重"这件事从存储层的硬约束，变成了写入路径的策略——人工录入不允许，确定性 ETL 允许。

## 设计 B：展示名支持多列拼接

`EntityMapping.standard_name_column: str` 扩展为 `standard_name_parts: list[str]`，多列以 ` / ` 连接（`William Jackson / 72848`）。分隔符取一个不会出现在业务值里、且在界面上易读的形式；不用冒号，避免和 `node_key` 的冒号分隔混淆。

向后兼容：`standard_name_column` 保留为单列写法的语法糖，配置解析时归一化成单元素的 `standard_name_parts`。已有的租户配置一行不用改。

**这一项不是界面美化，是设计 A 能落地的前提。** 只放宽唯一性而不做复合展示名，管理后台的实体列表里会出现两条一模一样的 `William Jackson`，人工审核和编辑都无法操作——放宽的收益拿不到，只拿到了坏处。

## 设计 C：寻址从 standard_name 改为 node_key

一旦同类型允许重名，`PUT /api/admin/{tenant_id}/terms/{standard_name}` 这条路径本身就有歧义。这一项是被设计 A 强制的，不是可选项。

改动点已经清点完毕，范围可控：

- `get_term(conn, tenant_id, standard_name, term_type)` 只有 **4 个调用点**：`admin_terms_routes.py:196`、`:277`，以及 `terms_store.py:535`（`update_term` 内部）、`:578`（`migrate_term_type` 内部）。新增按 node_key 定位的 `get_term_by_node_key`，四处改为调用它。
- 路由路径参数从 `{standard_name}` 改为 `{node_key}`，`term_type` query 参数随之取消（node_key 已含类型前缀，不需要额外消歧）。
- 前端 `termsApi.ts::updateTerm/deleteTerm` 的入参从 `currentStandardName` 改为 `nodeKey`，`TermRecord` 补 `node_key` 字段（后端 `TermResponse` 目前不返回它，需要一并补上）。
- `merge_terms` 不受影响——它早已按 `keep_node_key`/`merged_node_key` 寻址。

## 设计 D：消歧失败必须可区分

`resolve_term` 在命中两条及以上时返回 `None`（`ontology.py:86-97`），这个策略本身是对的——绝不从多个候选里随便选一个。问题在调用方怎么处理这个 `None`。

`structured_filter_query.py:630-632`：

```python
term = resolve_term(args.anchor.name, terms, term_type_hint=args.anchor.type_hint)
if term is None:
    return {"matched_count": 0, "anchors": []}
```

**"没找到"和"有歧义"被压成了同一个"0 条结果"。** Planner 拿到 `matched_count: 0`，会告诉用户"没有找到相关订单"——跟真的是零完全无法区分。

全局放宽唯一性会让歧义变多，也就让这个静默失败变多。所以这一项不是附带改进，是让设计 A 能安全落地的前提：

- 新增 `resolve_term_or_candidates(name, terms, *, term_type_hint)`，返回 `Term | list[Term]`——唯一命中返回 Term，多候选返回候选列表，零命中返回空列表。`resolve_term` 保留为它的薄封装（多候选时返回 `None`），既有调用方不受影响。
- `structured_filter_query` 的锚点解析改用新函数。多候选时返回结构化的歧义观察结果（列出候选的 `node_key`/`standard_name`/区分性属性），而不是 `matched_count: 0`。
- 工具的 `_USAGE_GUIDE` 相应说明：拿到歧义结果时应当向用户澄清是哪一个，不能当作"没有"。

零命中仍然返回 `matched_count: 0`，语义没变。

## 迁移与回滚

索引变更是纯放宽，**没有数据迁移**：不会有任何已存在的行因为这次改动变得不合法。

回滚需要注意：如果放宽后真的写入了同类型同名的多条 Term，重建唯一索引会失败。回滚前需要先跑一次同名检测（`SELECT tenant_id, term_type, standard_name, count(*) FROM terms GROUP BY 1,2,3 HAVING count(*) > 1`）并人工处理。这是一个单向门，实施前需要知情。

## 未决风险

- **全局放宽比按类型放宽风险大，这是知情的选择。** 评估时提出过按 `term_type` 声明"名字是否必须唯一"的方案：公司/产品/类目这类"按名字被查询"的类型保持唯一，客户/订单这类"只被遍历到"的类型放宽。选择全局放宽意味着 `resolve_term` 对**所有**类型都可能遇到多候选，包括今天靠唯一性保障可解析的公司/产品。缓解手段是设计 B（ETL 写入的展示名带判别列，实际上不会重）和设计 C/D（人工路径仍拦截、歧义可见）。但"Coca-Cola 有多少订单"这类查询在唯一性不再由数据库保证之后，正确性依赖的是这三层策略而不是一条硬约束——这个转变是真实的，需要实施任务用测试把三层都钉住。
- **`get_term` 的无 term_type 调用形态要一并清理。** 它的 docstring（`terms_store.py:350-356`）已经说明不传 `term_type` 时"多个同名不同类型的术语存在时返回其中任意一条（哪条由 SQLite 的行序决定，不保证稳定）"。放宽之后这个不确定性会从"跨类型"扩大到"同类型内"。改为按 node_key 寻址正好消除它，但实施时要确认没有遗留的无 `term_type` 调用。
- **ETL 重跑的展示名会变。** demo 租户的 `用户名` 从 `William Jackson` 变成 `William Jackson / 72848`，任何硬编码了旧展示名的测试/演示脚本需要同步。
- **本设计不解决"同名是否为同一实体"的判断。** 复合展示名让两个 William Jackson 在界面上可区分，但如果他们其实是同一个人搬了家，本设计会把他当成两个客户。那属于实体消歧，走 `duplicate_review_queue`，不在本次范围内。

## Global Constraints

- `node_key` 的生成规则、`node_key_parts` 配置形状、`compute_node_key` 的实现均不改动。
- `resolve_term` 的既有签名和"多候选返回 None"语义保持不变；新增能力通过新函数提供，不修改既有调用方的行为。
- Neo4j 侧不做任何改动——`sync_term`/`merge_relation` 早已按 `node_key` MERGE。
- 人工录入路径（`create_term`/`update_term`）保留 `_check_name_conflict`；只有 `upsert_term_with_node_key` 去掉这个检查。
- `standard_name_column` 单列写法必须继续可用，已有租户配置零改动。
