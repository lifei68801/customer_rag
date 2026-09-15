# 数据源解析选项：把"这张表怎么读"从硬编码变成配置

**状态**：设计
**日期**：2026-09-15

## 背景

MUJI 的 `CN_001_SKU_MASTER_121.xls`（908 行 × 113 列的 SKU 主数据表）今天**完全走不通**本项目的任何一条表格链路。实测：

| 链路 | 结果 |
|---|---|
| 表格导入（`etl_staging.read_table_rows`） | 113 列被读成 6 列，902 条 SKU 全部变成垃圾行 |
| 本体引导构建（`guidedOntology/columnStats.ts`） | 同样的 6 个废列名，推不出任何实体类型 |

根因有两条，都不是"解析写得不够聪明"：

**根因 1：解析决策硬编码在读取那一刻。** `_read_xls_rows` 写死 `sheet_by_index(0)` 和"第 0 行是表头"。这张表第 0 行是合并标题带（`Product Information` / `Order Information`…），**真正的字段名在第 6 行**（`md_no`、`jan`、`color`…），第 3 行是英文名，第 5 行是"Character Limit"说明行，数据从第 7 行开始。同一个 sheet 里叠了四层表头。

这个决策没有名字、没有存储、不可复查、不可重跑——用户唯一能做的是把 Excel 手工改成我们能读的形状。

**根因 2：解析器有两份，各自硬编码。** 后端 `etl_staging.py` 一份，前端 `schemaEtlConfigBuilder/tableHeader.ts` + `guidedOntology/columnStats.ts` 一份。两份都写死"第一个 sheet、第一行表头"，且规则会悄悄分叉——**2026-09-15 的 `9c71cf9`（后端重名列去重）已经让它们分叉了**：同一张带重名列的表，后端产出 `Color` / `Color (2)` 两个键，前端仍然显示两个 `Color`，用户在映射里选的 `Color` 指向第一列，第二列在界面上**无法寻址**。这个缺陷由本 spec 一并修掉。

## Foundry 是怎么做的

四条直接适用：

**上传只存字节，不产生行。** Raw dataset 收下文件后原样存储（带事务和版本），不解析、不猜表头、不产生任何一行。

**Schema 是事后贴上去的元数据，可以重贴。** CSV 的 schema 推断对话框能调分隔符、引号符、编码、跳过行数、各列类型；推错了改参数重贴，**原始文件一个字节没动**。

**脏格式走 transform，不走对话框。** Foundry 不把 `.xlsx` 当一等解析格式；多 sheet、合并标题带、表头在第 6 行这类事情的标准做法是写一个 transform 从 raw dataset 里读文件、挑 sheet、挑表头行、输出干净数据集。对应 roadmap 里引的那句：「Cleaning and formatting should be done upstream in data transformations, not the Ontology」。**MUJI 这张表的整理属于 Datasource 层，压根不该出现在本体映射那一步。**

**推断只负责提议，人负责确认。** 推断出的 schema 落在一个可编辑的对话框里，不是直接生效。

**不适用的一条**：Foundry 的解析是平台产生的，浏览器不参与。我们有一条 Foundry 没有的链路——本体引导构建要在**上传之前**就在浏览器里扫全表推列类型。这条差异是本 spec 最主要的设计压力来源，见"两份解析器"一节。

## 目标

1. 引入 `SourceParseOptions`（选 sheet、选表头行、选首数据行），让 MUJI 这类表不改 Excel 就能读对。
2. 解析选项**跟字段映射一起存下来**，重跑用同一份，不是上传时问一次就丢。
3. 表头行自动探测，结果作为**建议**呈现给用户，用户可改。
4. 消除前后端解析规则分叉，先补上 `9c71cf9` 造成的那一处。

## 非目标

- **不做列重命名。** 去重后缀（`Color (2)`）足以让用户在映射里分辨两列。允许改名会让"存着的映射引用的列名"多出一层间接，而这一层今天没有需求撑着。
- **不做多 sheet 合并、跨 sheet join、公式求值。** 那是通用数据平台，roadmap 的"明确不做的事"已经排除。
- **不引入持久的 raw dataset。** 见"未决风险"——这是正确的长期方向，但它会动上传流程、run 目录结构和 `promote_dry_run`，不该跟本 spec 捆在一起交付。
- **不动 `List` sheet 那类枚举字典的用法。** MUJI 文件里的 `List` sheet 是取值域字典，怎么用是本体侧的问题，不是解析侧的。

## 架构

```
选文件 ─▶ 解析设置 ─▶ 列名 + 样例行 ─▶ 字段映射 ─▶ 跑批
          sheet             ▲                      ▲
          表头行            └── 同一份 SourceParseOptions ──┘
          首数据行
```

`SourceParseOptions` 是 staging 层的输入，**不进 projection、不进本体**。它回答的是"这张表怎么读"，跟"读出来的列怎么映射到本体"是两个问题——今天它们挤在同一步里，正是 MUJI 这张表暴露出来的问题。

### 形状

```python
@dataclass(frozen=True)
class SourceParseOptions:
    sheet: str | int | None = None   # sheet 名或 0-based 序号；None = 第一个
    header_row: int = 1              # 1-based，跟 Excel 行号对齐
    first_data_row: int | None = None  # 1-based；None = header_row + 1
```

配置里的写法（`schema_etl_config` YAML 顶层新增 `sources:` 段）：

```yaml
sources:
  - file: CN_001_SKU_MASTER_121.xls
    sheet: Master
    header_row: 6
    first_data_row: 7
```

**为什么 `header_row` 用 1-based 绝对行号，而不是"跳过 N 行"**：用户是对着 Excel 看的，Excel 的行号从 1 开始。相对计数要用户自己做减法，而预览界面要高亮"就是这一行"时，绝对行号能直接用。

**为什么 `first_data_row` 是独立字段而不是 `header_row + 1`**：MUJI 这张表在表头和数据之间夹着说明行（第 5 行"Character Limit"）。如果用户选英文名那一行（第 3 行）当表头，第 4~6 行都得跳过。缺省值 `header_row + 1` 覆盖了绝大多数表，这个字段只在夹层存在时才需要填。

**为什么 `sheet` 允许名字或序号**：名字可读、能自解释，但 sheet 可能被重命名；序号稳定但看不出是哪张。两个都收，缺省仍是第一个——跟今天行为一致。

### 缺省值 = 今天的行为

`SourceParseOptions()` 的全部缺省值必须让 `read_table_rows` 的行为跟本 spec 实施前**逐字节一致**。已经存着的映射里没有 `sources:` 段，按缺省解释，不需要迁移。这条是硬约束，不是设计余地。

### 表头行自动探测

**只产出建议，永远不自动生效。** 界面显示"猜的是第 6 行"，用户可改——这条照搬 Foundry 的"推断提议、人确认"。

规则（必须确定、可测，不能是"看起来像"）：

- 候选范围：前 20 行。
- 每行打分 = `该行非空单元格数` × `其后 5 行的平均非空率`。
- 取分最高的行；并列取最靠上的。
- 全表少于 2 行时直接返回第 1 行。

在 MUJI 的 `Master` 上：第 0 行 6 个非空、第 2 行（英文）约 110、第 5 行（系统代码）113 —— 选中第 6 行（1-based），正确。在任何普通的"第一行就是表头"的表上，第 1 行非空数最高，退化成今天的行为。

乘上"其后几行的非空率"是为了排掉夹在中间的说明行：`Character Limit` 那一行本身填得挺满，但它下面紧跟着的行同样满，分数拼不过真表头——**这条需要一个专门的测试用例，用 MUJI 的真实行形状**，否则这个乘法项等于没写。

### 后端预览端点

```
POST /admin/schema-etl/source-preview
  multipart: file, 可选 options(JSON)
  →  { sheets: [...], detected: {header_row, first_data_row},
       columns: [...], sample_rows: [...] }
```

`columns` 是**经过重名去重之后**的列名——也就是跑批时真正会用的键。这是消除前后端分叉的锚点：前端展示什么、写进映射的是什么，都以这个返回为准。

### 两份解析器怎么办

这是本 spec 最难的一处，因为两边都有存在理由：后端是跑批的权威，前端要在上传前就扫全表做本体引导。

**决策：后端是权威，前端保留本地解析，但两者必须在同一份规则下，并且在上传时对账。**

1. 前端把 `tableHeader.ts` 和 `columnStats.ts` 里的解析部分收敛成一个模块 `sourceParser.ts`，接受同一份 `SourceParseOptions`，并实现同一套重名去重规则。今天这两个文件各自 `SheetNames[0]` + `rows[0]`，规则重复了两遍。
2. 去重规则用**一份共享 fixture** 锁住：`tests/fixtures/header-dedup-cases.json`，后端 pytest 和前端 vitest 都读它跑同一组用例。语言不同没法共享代码，但可以共享判据——这是唯一能真正防住分叉的手段，而不是靠两边各自写注释提醒对方。
3. 提交跑批时，前端把它本地算出的列名一并发给后端；后端用自己解析出的列名对账，**不一致就整体失败并列出差异**，而不是按后端的悄悄跑。理由跟 roadmap 里那条一样：Foundry 的主键重复从"appear as successful"演进成了 build 失败，我们这里也必须让分叉可见。

三条缺一不可：只有 1 和 2 时，分叉在下次有人改动某一边时仍会发生，只是晚一点；3 是唯一在运行时能抓住它的。

## 测试策略

- `SourceParseOptions()` 缺省值下，现有 staging 测试**一条都不改**就必须全绿——这是"缺省=今天行为"那条约束的判据。
- 用 MUJI 的真实行形状造夹具（不是把真文件放进仓库）：四层表头 + 说明行 + 113 列含重名，验证 `header_row: 6` 能读出 113 个正确列名、902 行数据。
- 表头探测的乘法项必须有专门用例：一个"说明行比真表头更满、但其后没有数据"的表，验证不会选中说明行。变异测试：去掉乘法项，这条用例必须变红。
- 前后端对账：造一个前端列名跟后端不一致的请求，验证跑批整体失败且差异出现在报告里。
- 共享 fixture 的两边用例数必须相等——任何一边漏跑几条都等于没对齐，这一条本身需要一个断言。

## 未决风险

- **前端仍然有一份解析器。** 对账机制能让分叉可见，但不能让它不发生。真正的解法是引入持久的 raw dataset（上传即落盘拿 `source_id`，之后预览、列统计、跑批都引用它，浏览器完全不解析），也就是 Foundry 的形状。那是下一份 spec，本 spec 的对账机制是它落地之前的防线。
- **xls 仍然整份读进内存。** `xlrd` 没有流式模式，本 spec 不改这一点。MUJI 主数据表 1.4MB / 908 行没问题，但设计文档给的真实规模是"18 万+ 行"——如果那个规模的表是 xls 而非 csv，这里会是瓶颈。
- **`first_data_row` 之前、`header_row` 之后的行被静默丢弃。** 这是设计意图（说明行），但如果用户把 `first_data_row` 填大了，会安静地少读数据。预览里必须显示"将跳过第 X~Y 行"，让这个丢弃是可见的。
- **表头探测在"表头确实就在第一行、但第一行有空列"的表上可能选错。** 打分规则偏好非空多的行。缓解手段只有"建议而非自动生效"这一条；如果实测中误判率高，规则需要重新设计，而不是加特例。

## Global Constraints

- `SourceParseOptions` 的缺省值必须使 `read_table_rows` 的行为与本 spec 实施前完全一致；现存映射不做迁移。
- staging 层对 csv/tsv/xlsx 保持流式产出，不因为要支持 `header_row` 而退化成全量读入。
- 重名列去重规则（`deduplicate_header`，`9c71cf9`）不改动，只是让前端跟上。
- 解析选项属于 staging 层，不得进入 `SchemaETLConfig` 的 entities/relations，也不得进入本体。
- 前后端列名不一致时整体失败，不允许"按后端的跑"。

## 参考

- `docs/superpowers/specs/2026-08-30-foundry-alignment-roadmap.md` —— Foundry 调研结论与现状对照
- `docs/superpowers/specs/2026-08-30-etl-layered-pipeline-design.md` —— 三层管道，本 spec 只动第一层
- 决策「固定读第一个 sheet」—— 原出处 `2026-08-21-schema-etl-multi-format-upload.md` 已在 `354cabe` 删除，
  该决策目前只活在代码注释里（`etl_staging.py::_read_xlsx_rows`、`frontend/.../tableHeader.ts`），
  两处都还指着这个不存在的文件。**本 spec 推翻这个决策**，实施时顺手把那两处失效引用改掉。
- `9c71cf9` —— 重名列去重，本 spec 承接它造成的前后端分叉
