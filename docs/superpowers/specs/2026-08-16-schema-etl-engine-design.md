# schema_etl.py 设计——ETL 写入引擎与稳定码注册机制

> 状态：设计定稿（经 grill-me 逐题确认）
> 前提：在 [2026-08-15《ETL 驱动的知识图谱 Schema 构建》架构设计](2026-08-15-etl-driven-schema-construction-design.md)（下称"架构文档"）第 7 节基础上细化。架构文档确定了 `schema_etl.py` 的职责边界（读取已确认 schema + 列映射配置，做确定性写入），但明确把"列映射配置具体长什么样""稳定码怎么分配"列为"留待实施计划阶段设计"——本文档就是那个设计，供后续 `writing-plans` 直接引用，不再需要临场决策。
> 关联文档：[2026-08-14 本体 schema 设计](2026-08-14-ontology-schema-design.md)、[2026-08-16 extra_fields 类型化](../plans/2026-08-16-extra-fields-typing.md)（已实现，本设计直接依赖其 `value_type` 声明）。

---

## 0. 这份文档解决什么问题

架构文档第 7 节给 `schema_etl.py` 划了范围：按已 confirm 的 schema + 列映射配置，做确定性写入；需要一个稳定码注册机制。本文档把这些留白填满：

1. 源数据到底以什么形态进入这个系统（第 1 节）
2. 列映射配置的完整格式（第 2 节）
3. 稳定码怎么分配、存在哪、并发假设是什么（第 3 节）
4. CSV/JSON 里的原始字符串怎么变成 `extra_properties` 要求的类型化值（第 4 节）
5. `terms_store.py` 需要新增什么写入接口（第 5 节）
6. 写入引擎的执行模型：阶段顺序、前置门禁、单行容错、汇总报告（第 6 节）

---

## 1. 源数据形态

**结论：文件（CSV/JSON/Parquet），`schema_etl.py` 只读文件，不直连任何外部数据库。**

MUJI 这类租户的主数据（`spu_products`/`sku_master`）活在其自己的数据库里。让 `schema_etl.py` 直连外部数据库需要引入对应数据库驱动依赖、管理跨系统凭据、处理网络连通性——这个系统能直接读客户的业务数据库，安全边界模糊、耦合面大。改成"客户方导出文件到约定目录/对象存储，`schema_etl.py` 只读文件"，边界清晰，不需要为每个可能的客户数据库类型引入新驱动。代价是多一步导出环节，数据时效性取决于客户导出频率——对 MUJI 这类批量、非实时的商品目录场景，这个代价可以接受。

---

## 2. 列映射配置格式

**结论：声明式 YAML，与 `app/graphrag/terminology_seed.yaml` 同风格**（这是本项目已经确立的配置文件格式）。业务/技术支持人员能直接编辑，不需要写 Python；代价是表达能力有上限（只能做字段映射，不能写任意转换逻辑）——如果未来出现"需要在映射时做复杂数据清洗"的真实需求，再单独评估要不要引入一层 Python 钩子，现在不做。

### 2.1 完整结构

```yaml
tenant_id: muji

entities:
  - term_type: Product
    source_file: products.csv
    product_line: "MUJI"                          # 固定字面量，不按行取值（见 2.4）
    standard_name_column: product_group_name       # 映射到 Term.standard_name
    node_key_parts:
      - column: product_group_id                  # 直接取列值——源系统自带稳定 ID
    field_mappings:                                # extra_properties 字段名 -> 源列名
      md_no: md_no
      sku_count: sku_count

  - term_type: SKU
    source_file: skus.csv
    product_line: "MUJI"
    standard_name_column: jan
    node_key_parts:
      - column: jan
    field_mappings:
      price: price

  - term_type: VariantValue
    source_file: variant_values.csv
    product_line: "MUJI"
    standard_name_column: label_cn
    node_key_parts:
      - column: dim_code                          # 维度码已经稳定，直接取列值
      - allocated_code:
          scope_columns: [dim_code]                # 作用域 = 同一 dim_code 下
          raw_value_column: raw_value              # 按这一列的值分配/复用稳定码
    field_mappings:
      value_kind: value_kind
      numeric_value: numeric_value
      dims: dims

relations:
  - relation_type: HAS_SKU
    source_file: skus.csv
    subject_term_type: Product                    # 复用 Product 自己的 node_key_parts 定义，
    object_term_type: SKU                          # 去这个文件里找同名列算 node_key（见 2.3）
```

### 2.2 `node_key_parts`：两种元素类型

每个 `entities` 条目的 `node_key_parts` 是一个有序列表，每个元素是以下两种之一：

- `{"column": "<源列名>"}` —— 直接取该列的原始值参与拼接。适用于源系统本来就有稳定 ID 的字段（`product_group_id`、`jan`、`dim_code`）。
- `{"allocated_code": {"scope_columns": [...], "raw_value_column": "..."}}` —— 按 `raw_value_column` 的值查/分配稳定码（见第 3 节），`scope_columns` 决定分配的命名空间。适用于源系统只有原始文本、没有现成稳定 ID 的字段（`value_code`）。

`node_key` 的最终值 = 各 `node_key_parts` 元素解析出的值按 `:` 拼接（`Product` → `"{product_group_id}"`；`VariantValue` → `"{dim_code}:{allocated_value_code}"`）。这个拼接结果对应架构文档第 5.3 节 `node_key_template` 里描述的模式（如 `"Variant:{dim_code}:{value_code}"`）——`term_type` 前缀由写入引擎在拼接结果前自动加上，配置里不用重复写。

### 2.3 `relations`：不重复声明 key 列，复用实体自己的 `node_key_parts`

`relations` 条目**不**单独声明 `subject_key_columns`/`object_key_columns`。写入引擎处理一条关系记录时，直接复用 `subject_term_type`/`object_term_type` 在 `entities` 段已经声明的 `node_key_parts` 定义，去关系数据所在的文件里找**同名列**算出对应的 `node_key`。

这要求关系文件里的列名与被引用实体的 `node_key_parts` 所需列名保持一致（`HAS_SKU` 的例子里，`skus.csv` 本身既是 SKU 实体的数据源、也是这条关系的数据源，列名天然一致；如果关系数据和实体数据分在不同文件、列名不一致，需要业务方在导出时统一命名）。**明确不做**列名重映射层——现在没有真实场景要求这个灵活性，等出现列名对不上的真实客户再评估要不要加。

### 2.4 `product_line`：固定字面量，不按行取值

`Term.product_line` 是硬性必填字段（`terms` 表 `NOT NULL`），受全局枚举（`ontology_product_lines`）约束——这两点在 [Term 多租户基础设施计划](../plans/2026-08-15-term-tenant-scoping-foundation.md) 里都没有改动。但 MUJI 这类 ETL 租户的源数据模型里根本没有"product_line"这个概念（MUJI 自己的品类体系是 `Category`，不是 `product_line`）——这个字段对 ETL 租户而言本质上是历史遗留的形式占位符，不是真正的业务概念。

因此每个 `entities` 条目的 `product_line` 是配置里的固定字面量（如 `"MUJI"`），不是按行从源列取值——一个 `term_type` 在一次 ETL 配置里只对应一个 `product_line` 值。写入前需要业务方先通过后台把这个值注册进全局 `ontology_product_lines` 枚举（复用 [本体基座计划](../plans/2026-08-15-term-tenant-scoping-foundation.md) 已有的 `create_product_line` 接口），否则会在 `_validate_categories` 校验时被拒绝。

### 2.5 `field_mappings` 与已确认 schema 的关系

`field_mappings` 只做"源列名 → `extra_properties` 字段名"的映射，不重复声明字段的 `value_type`——`value_type` 已经在业务方通过后台 UI 确认 schema 时注册进 `ontology_term_types.extra_fields`（见 [2026-08-16 extra_fields 类型化计划](../plans/2026-08-16-extra-fields-typing.md)），写入引擎运行时直接查这份已确认声明来做类型转换（见第 4 节），配置里不重复。这样 `value_type` 只有一处权威来源，不会出现"YAML 里写的类型"和"后台 UI 里注册的类型"对不上的问题。

---

## 3. 稳定码注册机制

**结论：有状态的注册表 + 按作用域自增计数，不用确定性哈希。**

哈希方案（`stable_code = hash(scope + raw_value)`）更简单、无状态、无并发分配竞争，但有一个致命限制：如果源系统后续修正了某个原始值的拼写（比如"抹茶"改成"抹茶味"），哈希会算出一个全新的码，这个实体在图谱里会被当成新实体——而这正是整个稳定码机制要解决的问题（身份不该随显示文本变化）。有状态注册表额外提供了一条人工纠正的后路：未来如果确实发生了这种拼写修正、需要把新旧文本关联到同一身份，可以手工在注册表里把 `raw_value` 列改成新文本、`stable_code` 不变——哈希方案完全做不到这一点。多维护一张表的代价换来这个长期正确性保证，值得。

### 3.1 表结构

```sql
CREATE TABLE etl_stable_code_registry (
    tenant_id    TEXT NOT NULL,
    scope        TEXT NOT NULL,
    raw_value    TEXT NOT NULL,
    stable_code  TEXT NOT NULL,
    allocated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, scope, raw_value)
);
```

`scope` 的值由写入引擎按 `term_type` + `scope_columns` 各列在当前行的值拼接生成（如 `"VariantValue:dim_007"`），不是配置里的字面量——`scope_columns` 只声明"用哪些列参与作用域划分"，实际的 scope 字符串在处理每一行时动态算出。

### 3.2 分配算法

```
给定 (tenant_id, scope, raw_value)：
  SELECT stable_code FROM etl_stable_code_registry
  WHERE tenant_id=? AND scope=? AND raw_value=?

  命中 → 直接复用这个 stable_code
  未命中 →
    SELECT COUNT(*) FROM etl_stable_code_registry WHERE tenant_id=? AND scope=?
    stable_code = f"{count + 1:05d}"   -- 该 scope 下从 1 开始的五位数序号
    INSERT INTO etl_stable_code_registry (...) VALUES (..., stable_code, now)
```

`stable_code` 的最终形态是纯数字序号（`"00001"`），不含 `scope` 前缀——`scope` 已经通过 `(tenant_id, scope)` 复合主键隐含在查询条件里，`node_key` 拼接时 `scope`（如 `dim_code`）作为独立的 `node_key_parts` 元素已经出现过一次，不需要在 `stable_code` 里重复。

### 3.3 并发假设

**本设计假设同一租户的 ETL 任务串行执行，不支持同一租户的多个 ETL 进程并发写入同一个 `scope`。** "查询未命中 → 插入新分配"这两步之间没有加锁，并发场景下会有竞争条件（两个进程同时给同一个新 `raw_value` 分配，可能产生同一个 `stable_code` 分配给两个不同 `raw_value`，或者同一个 `raw_value` 被分配两个不同 `stable_code`）。这是刻意的 YAGNI：当前没有"同一租户需要并发跑多个 ETL 任务"的真实场景，为一个不存在的场景加锁/重试逻辑是过度工程。如果未来出现这个需求，用 SQLite 的 `BEGIN IMMEDIATE` 事务或者应用层锁都能解决，到时候再加。

---

## 4. 类型转换：CSV/JSON 字符串 → `extra_properties` 要求的类型

**结论：写入引擎运行时查该字段在已确认 schema（`ontology_term_types.extra_fields`）里声明的 `value_type`，据此自动转换列值。**

配置里不重复声明类型（第 2.5 节已说明理由）。转换规则：

| `value_type` | 转换方式 | 说明 |
|---|---|---|
| `"string"` | 原样保留 | 无需转换 |
| `"number"` | `float(raw)` | `raw` 已经是数字类型（JSON 源）时跳过转换，直接用 |
| `"integer"` | `int(raw)` | 同上 |
| `"number[]"` | 按分隔符拆分后逐个转 `float` | CSV 里数组类字段的表示方式（分隔符、是否需要去除空白）由列映射配置的字段级选项决定，具体格式留给实施计划阶段设计——本文档只确定"最终产出是 `list[float]`"这个约束，不确定 CSV 里数组的具体字面表示 |

转换失败（比如 `value_type="number"` 但列值是 `"不是数字"`）按第 6.4 节的单行容错策略处理：跳过该行、记录日志，不中断整批。

---

## 5. terms_store.py 新增写入接口

**结论：新增 ETL 专用的写入函数，不改造现有的 `create_term`/`update_term`。**

现有 `create_term`/`update_term`（[2026-08-15 Term 多租户基础设施计划](../plans/2026-08-15-term-tenant-scoping-foundation.md) 的产物）在创建时强制 `node_key = standard_name`——这是专门为"抽取模式没有外部稳定码来源"设计的规则（该计划 Global Constraints 明确写了这一条）。ETL 模式的 `node_key` 是按 `node_key_template`/`node_key_parts` 拼出来的，跟 `standard_name` 完全独立，两种语义不兼容，不适合共用同一套函数硬塞一个可选参数。

新函数的语义要求：

- 按 `(tenant_id, node_key)` 做冲突判定，不是按 `standard_name`——已存在就更新属性，不存在就插入，是幂等 upsert，不是"创建 xor 更新"两态分支。这与 Neo4j 侧 `merge_relation`/`sync_term` 的 `MERGE` 幂等写入语义保持一致，ETL 重跑同一份数据不应该报错。
- `standard_name` 的租户内唯一性约束（`idx_terms_tenant_standard_name`）**仍然生效**，ETL 不能绕过——两个不同 `node_key` 的实体如果算出相同的 `standard_name`，数据库唯一索引会拒绝写入。这种情况按第 6.4 节的单行容错策略处理（跳过该行、记录日志），不是本函数需要特殊处理的场景。
- 具体函数签名、SQL 语句留给实施计划阶段编写（本文档只确定语义要求，不重复写实现）。

---

## 6. 写入引擎执行模型

### 6.1 入口形态

沿用本项目已确立的 CLI 入口惯例（`app/ingestion/main.py`/`incremental_main.py` 的模式）：`argparse` 解析参数，`async def main(...)` 接受可注入的依赖（settings、连接、client）方便测试，`if __name__ == "__main__":` 调用 `asyncio.run(main(...))`。具体参数（`--tenant-id`、`--config` 指向列映射 YAML 路径等）留给实施计划阶段设计。

### 6.2 前置门禁：schema 必须已确认

运行前检查 `is_ontology_confirmed(tenant_id)`（[本体基座计划](../plans/2026-08-15-term-tenant-scoping-foundation.md) 已有的函数），未确认直接拒绝运行、不写入任何数据。这与整个项目"抽取前必须先定义并确认本体 schema"的核心原则（[2026-08-14 spec](2026-08-14-ontology-schema-design.md)）完全一致——ETL 是另一条数据写入路径，没有理由例外。

### 6.3 处理阶段顺序

1. 按配置里 `entities` 的声明顺序，逐个 `term_type` 读取其 `source_file`，对每一行：计算 `node_key`（第 2.2/3 节）、按 `value_type` 转换 `field_mappings` 里的值（第 4 节）、调用第 5 节的 upsert 写入 SQLite + Neo4j `sync_term`。
2. **所有 entities 处理完之后**，再处理 `relations`——relations 引用的实体 `node_key` 必须已经存在，不能在实体尚未写入时就尝试建边。
3. 对每条 `relations` 声明，读取其 `source_file`，对每一行：按 subject/object 各自 `term_type` 的 `node_key_parts` 定义算出两端的 `node_key`，调用 Neo4j `merge_relation` 写入边（`tenant_id`+两端 `node_key` 幂等 MERGE，见 [Term 多租户基础设施计划](../plans/2026-08-15-term-tenant-scoping-foundation.md) Task 3）。

### 6.4 单行容错策略

**结论：跳过出错的单行、记录日志，继续处理其余行，最后汇总报告。**

ETL 源数据量级大（MUJI 一张 SKU 表 18 万+ 行），一行脏数据不该让整批任务失败——业务方更关心"这次写进去多少、跳过了哪些、为什么跳过"，而不是"一报错就全部中断，什么都没写进去"。

会被跳过并记录的情况包括：`node_key_parts` 引用的列在这一行不存在/为空、`field_mappings` 某个字段的值转换失败（第 4 节）、写入 SQLite 时触发 `standard_name` 唯一性冲突（第 5 节）、`term_type`/`relation_type` 不在已确认 schema 里。

汇总报告需要覆盖：每个 `term_type`/`relation_type` 成功写入的行数、跳过的行数及每一行跳过的具体原因（源文件名 + 行号 + 原因），供业务方核对源数据质量。报告的具体数据结构留给实施计划阶段设计。

### 6.5 重跑语义：只做新增/更新，不做旧边自动对账删除

**结论：ETL 写入只 MERGE（新增或更新），从不主动删除边或节点。**

要做到"源数据里消失的关系自动从图谱删除"，需要先查出"该租户现有的全部相关边"，再和新数据比对差异——查询成本高，而且一旦对账逻辑有 bug，会误删真实存在的数据，风险和收益不成比例。不删的代价是图谱会累积过时边（比如某个 SKU 不再关联某个变体值了，旧边还在），但这是可接受的风险，且符合项目一贯的 YAGNI 判断——没有真实场景证明"必须自动清理过时边"这个投入是必要的。业务方如果确实需要清理某条过时数据，走管理后台手动处理即可（[本体基座计划](../plans/2026-08-15-term-tenant-scoping-foundation.md) Task 4 已经有 `admin_terms_routes.py` 的删除接口）。

---

## 7. 范围之外（不做）

- **直连外部数据库读取源数据**——第 1 节已论证，只读导出文件。
- **YAML 配置里的任意转换逻辑/Python 钩子**——第 2 节已论证，`field_mappings` 只做字段名映射。
- **关系文件与实体文件之间的列名重映射层**——第 2.3 节已论证，要求列名一致，等真实客户遇到列名不一致的情况再评估。
- **稳定码分配的并发控制（锁/重试）**——第 3.3 节已论证，假设同租户 ETL 串行执行，没有真实并发场景支撑这个投入。
- **重跑时的旧边/旧节点自动对账删除**——第 6.5 节已论证，只做增量式 MERGE，清理走管理后台手动操作。
- **数组字段（`number[]`）在 CSV 里的具体字面表示格式**——第 4 节已标注，留给实施计划阶段确定分隔符等细节，不是架构层面的决策。

---

## 8. 待实施计划阶段确定的具体细节

以下事项本文档已经给出方向性约束，但具体实现留给 `writing-plans` 阶段：

1. `terms_store.py` 新写入函数（第 5 节）的确切函数签名、SQL 语句。
2. CLI 入口（第 6.1 节）的确切参数列表。
3. 汇总报告（第 6.4 节）的确切数据结构。
4. `number[]` 字段在 CSV 里的具体字面表示格式（第 4 节）。
