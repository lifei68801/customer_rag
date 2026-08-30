# ETL 分层管道与写入前主键校验

**路线图**：[2026-08-30-foundry-alignment-roadmap.md](2026-08-30-foundry-alignment-roadmap.md)（四份 spec 中的第二份，可独立交付）

## 背景

`schema_etl.py::_write_entity_mapping` 目前一边读文件一边写库：

```python
for row_number, row in enumerate(_read_table_rows(...), start=2):
    try:
        node_key = await compute_node_key(...)   # 现算
        ...
        await upsert_term_with_node_key(...)     # 立刻写
    except (RowProcessingError, TermNameConflictError, UnknownCategoryError) as exc:
        report.entities_skipped += 1
        _record_skipped_row(...)
```

`node_key` 从来没有以数据的形态存在过——它在循环里被算出来，立刻被消费掉。这带来两个后果：

**一、键的冲突只能在写入过程中暴露。** 2026-08-30 重建 demo 租户时，`用户名` 的 10000 行里有 665 行写不进去，表现为"跑到中途开始逐行跳过"，而且要等整轮跑完读报告才知道规模。写入是部分完成的：9335 条已落库，665 条没有。

**二、无法在写之前检查任何东西。** 想回答"这份配置算出来的键有没有重复"，唯一办法是真的跑一遍。

Foundry 不是这样做的。它的主键是背书数据集上的**一列**，本体只是指向它；复合键的官方解法是「define pipeline logic such that the primary key is the function of either a single column or multiple columns」——在管道里拼出来、物化成列。于是「check your backing datasources for duplicates before assigning a primary key」是一次普通的数据质量检查，不需要触碰本体。

主键重复在 Foundry 里也不是可跳过的行级问题：Object Storage v2 直接让 build 失败。旧的 v1 会「appear as successful; however, the duplicate primary keys can cause unexpected changes」——它自己从静默演进到了失败。

## 目标

- 把 ETL 拆成三层：**staging**（解析与类型归一）→ **projection**（物化 `node_key` 与展示名）→ **写入**。
- 主键重复在**写入之前**被发现，整体失败、零写入。
- 键与展示名成为可检查的中间数据，而不是循环里的临时变量。

## 非目标

- 不建通用 transform 平台（任意变换、血缘、构建编排）。只做本体背书这一条链路需要的最小分层。
- 不改 `compute_node_key` 的实现或 `node_key_parts` 的配置形状，只改变它的调用位置。
- 不改关系写入路径的结构。它已经有端点存在性守卫（2026-08-29 新增），本设计只让它消费 projection 的产物。
- 不引入持久化的中间数据集。三层都在一次进程内传递，不落盘——落盘是 Foundry 有数据平台才划算的做法。

## 架构

```
上传文件 ──(staging)──▶ 规范化行 ──(projection)──▶ 带键的行 ──(预检)──▶ 写入
 xlsx/csv/tsv/xls        dict[str,str]           ProjectedRow      查重      terms + Neo4j
                         统一类型                 node_key
                                                 standard_name
```

### 第一层 · staging：解析与类型归一

职责：把 xlsx/csv/tsv/xls 解析成统一的行序列，做类型归一（Excel 日期 → ISO 字符串、数值 → 字符串、公式错误单元格 → 空值等）。

这一层的逻辑**今天已经完整存在**，散在 `schema_etl.py` 里：`_read_table_rows`、`_read_delimited_rows`、`_read_xlsx_rows`、`_read_xls_rows`、`_detect_text_encoding`、`_xlrd_cell_to_python_value`，以及 `schema_etl_row_processing.py::convert_excel_cell_to_string`。

本设计把它们提取到 `app/graphrag/etl_staging.py`，**语义一行不改**——包括流式产出（`Iterator[dict[str, str]]`）、xlsx 幽灵行跳过、CSV 编码探测的 UTF-8/GBK 回退。只是从"ETL 引擎的内部细节"变成"管道的第一层"。

对应 Foundry 的 Datasource 层：基础清洗、统一 schema。

### 第二层 · projection：物化键与展示名

新建 `app/graphrag/etl_projection.py`：

```python
@dataclass(frozen=True)
class ProjectedRow:
    row_number: int                        # 源文件行号，报错定位用
    node_key: str
    standard_name: str
    extra_properties: dict[str, object]


@dataclass(frozen=True)
class ProjectionResult:
    rows: list[ProjectedRow]
    skipped: list[SkippedRow]              # 缺列、类型转换失败等行级问题
    duplicate_keys: dict[str, list[int]]   # node_key -> 出现行号，只收录出现 >1 次的


async def project_entity_rows(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    mapping: EntityMapping,
    extra_field_specs: dict[str, ExtraFieldSpec],
    data_dir: Path,
) -> ProjectionResult: ...
```

它做的就是今天写入循环里"算"的那一半：调 `compute_node_key`、取 `standard_name`、按声明的 `value_type` 转换 `extra_properties`。不同之处是**算完不写**，攒成结果返回。

### 一份文件背书多个对象类型，靠 projection 切开

Foundry 有一条硬规则：「Object types are backed by a single dataset, and a dataset can back only one object type.」

我们的配置直接违反它——demo 的 `soft_drink_sales.xlsx` **同时背书五个 term_type**（产品/公司/类目/用户名/订单号）。这是宽事实表的必然结果：一行流水里同时躺着订单、产品、公司、类目和客户。

**projection 层就是这条规则的落点。** 每个 `EntityMapping` 跑一次 projection，产出的 `ProjectionResult` 就是"该对象类型的背书数据"——一个文件在这一层被切成五份，每份只含一个对象类型需要的键、展示名和属性。物理上仍是一个上传文件，逻辑上已经是 Foundry 要求的 1:1。

Foundry 靠 clean → ontology 那一步 transform 做同样的事（「separating them allows you to add derived columns to the Ontology-backing dataset」）；我们没有数据平台，所以这一刀落在 projection 层。

这不只是概念上的对齐：[Spec 4](2026-08-30-source-deletion-propagation-design.md) 的实体清理正是按 `term_type` 圈定范围的，它成立的前提就是"每个 term_type 恰好对应一个背书数据源"。

`duplicate_keys` 是这一层存在的理由——Foundry 那句「check your backing datasources for duplicates」在本项目里的落点。

**`allocated_code` 的注意事项**：`compute_node_key` 在 `allow_allocation=True` 时会为首次出现的原始值分配稳定码并**持久化**（`etl_stable_code_registry`）。projection 层跑的是实体路径，仍然 `allow_allocation=True`，所以调用它本身有副作用。这意味着"预检失败、零写入"这个保证对 `terms`/Neo4j 成立，但**稳定码注册表已经写了**。这是可接受的：稳定码是幂等分配的（同一 scope + 原始值永远得到同一个码），下次重跑会命中已有分配，不会漂移。实施任务需要用测试钉住这一点。

### 第三层 · 写入：消费 projection 产物

`_write_entity_mapping` 改为接收 `ProjectionResult`，不再自己读文件。行为不变。

关系路径同理：`_write_relation_mapping` 目前也在循环里 `compute_node_key(allow_allocation=False)`，改为消费一个关系侧的 projection 产物。它的端点存在性守卫保持不变——那道守卫防的是"实体行被跳过导致端点缺失"，在分层之后依然必要（`skipped` 里的行就是这种情况）。

## 主键重复：整体失败

`run_schema_etl` 的新流程：

```
1. 对每个 EntityMapping 跑 projection
2. 汇总所有 duplicate_keys
3. 有重复 -> 抛 DuplicateNodeKeyError，零写入
4. 无重复 -> 逐个映射写入实体，然后写入关系
```

`DuplicateNodeKeyError` 的消息要能直接定位问题，列出冲突的 node_key 及其源文件行号（最多 20 条样例，避免刷屏，并注明总冲突数）：

```
实体类型 '用户名' 的 node_key 有 665 处重复，本次未写入任何数据。
配置里 node_key_parts 声明的列组合不足以唯一标识每一行，请检查：
  用户名:William Jackson  ← 源文件第 42, 3891 行
  用户名:Kimberly Lopez   ← 源文件第 77, 5012 行
  ...（另有 663 处，完整清单见运行报告）
```

**为什么是整体失败而不是跳过**：主键重复意味着这份配置的 `node_key_parts` 选错了——它没能唯一标识每一行。这不是"某几行数据脏"，是配置层面的错误，跳过多少行都不会让配置变对。部分写入反而留下一个"看起来成功了、实际缺了一部分"的图谱，比失败更难发现。这与 Object Storage v2 的选择一致。

行级问题（缺列、类型转换失败）**仍然是跳过 + 记报告**，语义不变——那才是真正的"某几行数据脏"。

## `ON CONFLICT DO UPDATE` 的语义澄清

`upsert_term_with_node_key` 在同一 `node_key` 被多行命中时静默取最后一行（`terms_store.py:653-656`）。预检落地后，**同一次运行内**的重复键根本进不到写入层，这个静默覆盖只会发生在**跨次运行**——同一个 node_key 在第二次运行里带了不同属性。那是正常的幂等更新语义（数据源变了，本体跟着变），保留不变。

这一条是澄清，不是改动。写进本 spec 是为了让"预检解决了什么、没解决什么"有明确边界。

## 测试策略

- **staging 层提取是纯搬运**：现有 `tests/graphrag/test_schema_etl.py` 里覆盖解析行为的用例（GBK 编码、UTF-8 BOM、xlsx/xls、幽灵行跳过、空 sheet）改为直接测 `etl_staging`，断言一行不改。搬运本身不该改变任何行为。
- **projection 层**：给定一份配置和数据，断言产出的 `node_key`/`standard_name`/`extra_properties` 与今天写入库里的值一致；断言 `duplicate_keys` 能抓出重复。
- **整体失败**：用一份 `node_key_parts` 不足以唯一标识的配置跑 `run_schema_etl`，断言抛 `DuplicateNodeKeyError`，且 `terms` 表**一行没写**、Neo4j 一条边没写。这条是本设计的核心保证。
- **行级问题仍然跳过**：缺列的脏行不触发整体失败，仍然计入 `skipped`。
- **稳定码副作用**：预检失败后，`etl_stable_code_registry` 里已分配的码在下次运行中被复用，不产生新码。

## 未决风险

- **projection 会引入一次全量内存驻留。** 现状是流式逐行（`_read_table_rows` 是生成器，设计文档给的真实规模是「MUJI 一张 SKU 表 18 万+ 行」）。全量查重必然要把所有 `node_key` 驻留内存；18 万个字符串键约几十 MB，可接受。但 `ProjectionResult.rows` 还带着 `extra_properties`，驻留量会大得多。实施任务需要决定：是只驻留键（查重）而行本身仍然流式二次读取，还是接受全量驻留。前者要读两遍文件，后者内存上界随行宽增长——**这个取舍必须在实施时明确，不能默认**。
- **稳定码的分配副作用穿透了"零写入"保证。** 见 projection 层的说明。保证的准确表述是"`terms` 和 Neo4j 零写入"，不是"零副作用"。
- **报告结构要变。** `ETLRunReport` 目前只有 `skipped_rows`/`skipped_mappings`，需要容纳"整体失败"这个新的终态。管理后台的 ETL 运行列表（`admin_schema_etl_routes.py`、`etl_runs_store`）展示逻辑要同步。
- **本设计不解决"两行算出同一个键但属性不同该信谁"。** 预检把这种情况整体拦下，等于要求配置改对。如果将来出现"合法地允许同键多行、取最后一条"的需求，需要重新设计——但那本质上是承认 `node_key_parts` 选得不对。

## Global Constraints

- `compute_node_key` 的实现与 `node_key_parts` 的配置形状不改动，只改变调用位置。
- staging 层的提取必须是纯搬运：编码探测、幽灵行跳过、类型归一的行为一行不改。
- 行级脏数据（缺列、类型转换失败）仍然是跳过 + 记报告；只有主键重复升级为整体失败。
- 关系写入路径的端点存在性守卫保持不变。
- 三层都在进程内传递，不引入持久化的中间数据集。
