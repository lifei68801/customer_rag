# 宽表接入 Schema ETL 指南

日期：2026-08-20

## 适用场景

源数据是一张"宽表"：一行代表一个主实体（比如一个 SKU），列里既有这个主实体自身的标识/属性列，也混着若干"维度"列（类目、颜色、材质等）。这些维度列的值既可能只是主实体的一个标签，也可能需要被当成独立的实体、能够反过来查询和聚合（"这个颜色下有多少 SKU"）。本文档说明：不改一行代码，仅通过 `config.yaml` 的写法，就能把这种宽表映射进符合本体 schema 设计的知识图谱存储结构。

背景机制见 `docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md`（`schema_etl.py` 的整体设计）；本文档是它的一个具体用法补充。

## 第一步：判断维度列该做属性还是做独立实体

不是风格选择，取决于你需要什么样的查询能力：

- **只需要"筛选/展示某个主实体的这个维度值是什么"**（比如"给我所有颜色=白色的 SKU"）→ 做成主实体的标量属性（`extra_fields`/`field_mappings`），一次 upsert 搞定，成本最低。
- **需要"从维度反查/聚合"**（比如"这个颜色下有多少 SKU"、"哪些材质总是和哪些颜色一起出现"、维度本身要挂载额外信息或走审核去重）→ 做成独立的 `term_type` + 关系，见下文。

默认走标量属性，只有能明确说出具体的反查/聚合查询场景时才升级成独立实体——独立实体的写入成本明显更高（见"性能"一节）。

## 第二步：如果做成独立实体，node_key 怎么定义

`node_key_parts` 直接用宽表列值本身（`{column: 该维度列}`）的前提是：**这一列是受控值域**（比如源系统里是下拉选择框，不存在"白色"/"白"两种写法并存）。

如果源数据是自由文本、存在同义变体，**必须在数据进入 ETL 之前先做一轮归一化**（清洗成统一值域）。`EntityMapping` 配置格式目前没有"别名/同义词"字段，ETL 本身不提供事中去重合并能力——写入路径上有多少种写法就会产生多少个互不相干的节点，直接破坏"聚合"这个诉求。

## 第三步：不要漏掉维度自己的 EntityMapping——否则会产出"幽灵节点"

只写"主实体→维度"的关系映射、不给维度单独声明 `EntityMapping`，是最容易踩的坑：

`neo4j_client.py::merge_relation` 的 Cypher 是 `MERGE (a:Term {...}) MERGE (b:Term {...})`——如果关系的客体节点在图里还不存在，Neo4j 会自动创建它，但只带 `tenant_id`/`node_key`，没有 `standard_name`/`type` 属性，而且这个节点完全不会出现在 SQLite 的 `terms` 表里（`merge_relation` 不碰 SQLite）。也就是说，维度节点要想成为"完整、可管理、有类型"的实体，**必须各自也有一条独立的 `EntityMapping`**，不能指望关系映射顺带把它建完整。

## 完整示例：SKU 宽表 → 类目/颜色/材质

假设宽表 `sku_wide.csv` 列为：`SKU编码, SKU名称, 类目, 颜色, 材质, 价格`，三个维度都需要反查聚合、且都是受控值域：

```yaml
tenant_id: your_tenant

entities:
  # 1. SKU 本体：node_key 用 SKU编码，价格作为标量属性挂在 SKU 上
  - term_type: SKU
    source_file: sku_wide.csv
    standard_name_column: SKU名称
    node_key_parts:
      - column: SKU编码
    field_mappings:
      价格: 价格列   # 本体声明的字段名: CSV 里的列名（两者允许不同名）

  # 2-4. 三个维度各自的实体映射——同一张宽表，只取各自需要的那一列
  - term_type: 类目
    source_file: sku_wide.csv
    standard_name_column: 类目
    node_key_parts:
      - column: 类目
    field_mappings: {}

  - term_type: 颜色
    source_file: sku_wide.csv
    standard_name_column: 颜色
    node_key_parts:
      - column: 颜色
    field_mappings: {}

  - term_type: 材质
    source_file: sku_wide.csv
    standard_name_column: 材质
    node_key_parts:
      - column: 材质
    field_mappings: {}

relations:
  # 5-7. 同一张宽表，同一行里 SKU编码 列算主体 node_key，
  # 类目/颜色/材质 列各自算客体 node_key
  - relation_type: BELONGS_TO_CATEGORY
    source_file: sku_wide.csv
    subject_term_type: SKU
    object_term_type: 类目

  - relation_type: HAS_COLOR
    source_file: sku_wide.csv
    subject_term_type: SKU
    object_term_type: 颜色

  - relation_type: HAS_MATERIAL
    source_file: sku_wide.csv
    subject_term_type: SKU
    object_term_type: 材质
```

上传时只需要提交这一份 `config.yaml` + 这一张宽表 CSV（`data_files` 只选这一个文件）。`run_schema_etl` 依次跑完这 7 条映射，每条各自去同一张宽表里取它需要的列。最终图里：SKU 节点（带价格属性）分别通过 `BELONGS_TO_CATEGORY`/`HAS_COLOR`/`HAS_MATERIAL` 三条边指向对应的类目/颜色/材质节点；同一个类目/颜色/材质值在多个 SKU 行里重复出现时会被 upsert 成同一个节点，不会重复建节点，天然支持反查/聚合。

**前提**：三个维度的 `term_type` 和三个 `relation_type`（连同 SKU→类目/颜色/材质这三组允许组合）都要先在本体 schema 里正式声明并确认（走 draft/confirm 流程）——ETL 只认已确认的 schema，和普通用法一致，没有额外要求。

## 性能：同一张宽表被扫描多次，要不要拆分预处理

上面的例子里，同一张宽表被 7 条映射各自完整扫描一遍。默认建议：**直接接受，不做拆分预处理**。

理由：CSV 逐行流式解析本身很快，真正的耗时大头是每一行触发的 SQLite upsert + Neo4j MERGE 网络往返——这部分开销无论怎么拆文件都省不掉（该写的节点、该写的边一条都不会少），文件扫描只是这个开销边上的一个小分量。拆分宽表能省的只是"重复解析同一段文本"，相对总耗时占比很小，却要多一步预处理流程、多一份中间产物要维护，性价比不划算。ETL 是一次性/低频的批处理任务，不是要优化到毫秒级的热路径。

如果宽表规模远超 MUJI 参考案例（18 万+ 行）的量级，且实测扫描本身（而非 DB/Neo4j 写入）成为瓶颈，再考虑拆分——不要提前优化。

## 与"多个实体类型共享一个文件"的区别（容易混淆）

这个模式（一个 `EntityMapping` + 多个 `RelationMapping` 共享同一个 `source_file`）和"两个不同 `term_type` 的 `EntityMapping` 共享同一个文件"是两回事，后者**不可行**：`_read_csv_rows` 把文件里每一行都当成对应映射类型的一条记录处理，两个实体类型共享一个文件会导致每一行被同时按两种类型解析，列名对不上大概率报错、凑巧对上也会产出语义错误的数据——这种情况必须拆成各自独立的文件。

本文档讲的场景不同：`EntityMapping` 把这一行当"一个节点"来读，`RelationMapping` 把这一行当"一条边两端的取值来源"来读，两者对同一行的解读方式不冲突，所以可以共享同一个物理文件。
