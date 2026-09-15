# 数据源解析选项 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"这张表怎么读"（选 sheet、选表头行、选首数据行）成为可配置、可存储、可重跑的选项，使 MUJI 那类多层表头的 Excel 不改文件就能走通表格导入和本体引导构建。

**Architecture:** 新增 `SourceParseOptions` 作为 staging 层（`etl_staging.py`）的输入；配置 YAML 顶层新增 `sources:` 段承载它，跟字段映射一起存进 `ontology_etl_mapping`；前端把两份重复的解析器收敛成 `sourceParser.ts` 并支持同一份选项，外加只在前端实现的表头行探测；跑批时后端用自己解析出的列名跟前端发来的对账，不一致整体失败。

**Tech Stack:** Python 3.12 / FastAPI / openpyxl / xlrd / PyYAML；React + TypeScript / SheetJS(xlsx) / Vitest

**Spec:** `docs/superpowers/specs/2026-09-15-source-parse-options-design.md`

## Global Constraints

- `SourceParseOptions()` 的全部缺省值必须使 `read_table_rows` 的行为与本计划实施前**逐字节一致**；现存映射不做迁移，没有 `sources:` 段就按缺省解释。
- staging 层对 csv/tsv/xlsx 保持**流式**产出，不得因为要支持 `header_row` 而退化成全量读入（xls 本来就是全量，不变）。
- 重名列去重规则（`deduplicate_header`，提交 `9c71cf9`）**不改动**，只是让前端跟上。
- 解析选项属于 staging 层，不得进入 `SchemaETLConfig.entities/relations`，也不得进入本体。
- 前后端列名不一致时**整体失败**，不允许"按后端的悄悄跑"。
- 不做列重命名；不做多 sheet 合并/跨 sheet join/公式求值；不引入持久 raw dataset。
- 后端**不实现**表头行探测——它的缺省永远是第 1 行。探测只在前端。
- 后端 pytest 必须 `python -u -m pytest`，后台跑并轮询日志。前端在 `frontend/` 下跑 `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2`，**不要**跑 `npx prettier --write`。
- 每个关键行为做变异测试：改坏实现，确认对应测试变红，再改回来。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `app/graphrag/source_parse_options.py` | **新建。** `SourceParseOptions` 数据类 + 校验 + `InvalidSourceParseOptionsError`。没有 IO，不认识文件格式。 |
| `app/graphrag/etl_staging.py` | **改。** 三个读取器接受 `SourceParseOptions`；新增 sheet 选择辅助函数。 |
| `app/graphrag/schema_etl_config.py` | **改。** `SchemaETLConfig.sources: dict[str, SourceParseOptions]`；YAML `sources:` 段的解析与摘要。 |
| `app/graphrag/etl_projection.py` | **改。** 三处 `read_table_rows` 调用点带上该文件的选项。 |
| `app/api/admin_schema_etl_routes.py` | **改。** 接收前端列名并对账，不一致返回 400。 |
| `fixtures/header-dedup-cases.json` | **新建。** 去重规则的共享判据，前后端测试各读一遍。 |
| `frontend/src/admin/schemaEtlConfigBuilder/sourceParser.ts` | **新建。** 前端唯一的解析入口，吃同一份选项，实现同一套去重。 |
| `frontend/src/admin/schemaEtlConfigBuilder/detectHeaderRow.ts` | **新建。** 表头行探测。纯函数，吃二维数组。 |
| `frontend/src/admin/schemaEtlConfigBuilder/tableHeader.ts` | **改。** 解析逻辑搬走，只留薄封装。 |
| `frontend/src/admin/guidedOntology/columnStats.ts` | **改。** 私有 `readTableRows` 删掉，改用 `sourceParser.ts`。 |
| `frontend/src/admin/schemaEtlConfigBuilder/types.ts` | **改。** `AddedFile` 加 `parseOptions`。 |
| `frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.ts` | **改。** 输出 `sources:` 段。 |
| `frontend/src/admin/schemaEtlConfigBuilder/storedParseOptions.ts` | **新建。** 存着的解析选项翻译回编辑器形状（snake_case/null → camelCase/undefined）。 |
| `frontend/src/admin/etlMappingApi.ts` | **改。** `EtlMappingSummary` 补 `sources`。 |
| `frontend/src/admin/schemaEtlConfigBuilder/TableImportFlow.tsx` | **改。** 第二步加「解析设置」面板；提交时带上列名。 |

---

### Task 1: 后端 `SourceParseOptions` 与 staging 支持

**Files:**
- Create: `app/graphrag/source_parse_options.py`
- Modify: `app/graphrag/etl_staging.py:77-98`（csv）、`:101-131`（xlsx）、`:153-174`（xls）、`:177-195`（分流）
- Test: `tests/graphrag/test_etl_staging.py`

**Interfaces:**
- Produces: `SourceParseOptions(sheet: str | int | None = None, header_row: int = 1, first_data_row: int | None = None)`、`InvalidSourceParseOptionsError`、`read_table_rows(path: Path, options: SourceParseOptions | None = None) -> Iterator[dict[str, str]]`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/graphrag/test_etl_staging.py`：

```python
from app.graphrag.source_parse_options import (
    InvalidSourceParseOptionsError,
    SourceParseOptions,
)


def test_read_table_rows_reads_the_named_sheet(tmp_path: Path):
    """固定读第一个 sheet 的老决策在这里被推翻：MUJI 的主数据表在 Master，
    同一个文件里还有 List / Work 等好几张表。"""
    path = tmp_path / "multi.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "Cover"
    first.append(["说明"])
    second = workbook.create_sheet("Master")
    second.append(["jan", "color"])
    second.append(["4934761229522", "Natural"])
    workbook.save(path)

    rows = list(read_table_rows(path, SourceParseOptions(sheet="Master")))

    assert rows == [{"jan": "4934761229522", "color": "Natural"}]


def test_read_table_rows_reads_the_sheet_by_index(tmp_path: Path):
    path = tmp_path / "multi.xlsx"
    workbook = Workbook()
    workbook.active.title = "Cover"
    workbook.active.append(["说明"])
    second = workbook.create_sheet("Master")
    second.append(["jan"])
    second.append(["4934761229522"])
    workbook.save(path)

    rows = list(read_table_rows(path, SourceParseOptions(sheet=1)))

    assert rows == [{"jan": "4934761229522"}]


def test_read_table_rows_reports_a_missing_sheet_by_name(tmp_path: Path):
    """静默回落到第一张表会让用户拿到一份完全不相干的数据，而且不报错。"""
    path = tmp_path / "multi.xlsx"
    workbook = Workbook()
    workbook.active.title = "Cover"
    workbook.active.append(["说明"])
    workbook.save(path)

    with pytest.raises(RowProcessingError, match="Master"):
        list(read_table_rows(path, SourceParseOptions(sheet="Master")))


def test_read_table_rows_takes_the_header_from_the_given_row(tmp_path: Path):
    """MUJI 的形状：第 1 行是合并标题带，第 2 行英文名，第 3 行说明，
    真表头在第 4 行，数据从第 5 行开始。"""
    path = tmp_path / "layered.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Product Information", None])
    sheet.append(["Article Code", "Color"])
    sheet.append(["-", "(half width 100)"])
    sheet.append(["md_no", "color"])
    sheet.append(["M1AG702", "Natural"])
    workbook.save(path)

    rows = list(read_table_rows(path, SourceParseOptions(header_row=4)))

    assert rows == [{"md_no": "M1AG702", "color": "Natural"}]


def test_read_table_rows_skips_the_notes_rows_between_header_and_data(tmp_path: Path):
    """用户挑了英文名那一行当表头时，它和数据之间还夹着说明行。"""
    path = tmp_path / "notes.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Article Code", "Color"])
    sheet.append(["-", "(half width 100)"])
    sheet.append(["md_no", "color"])
    sheet.append(["M1AG702", "Natural"])
    workbook.save(path)

    rows = list(read_table_rows(path, SourceParseOptions(header_row=1, first_data_row=4)))

    assert rows == [{"Article Code": "M1AG702", "Color": "Natural"}]


def test_read_table_rows_honours_header_row_for_csv(tmp_path: Path):
    path = tmp_path / "layered.csv"
    path.write_text("标题带,\nmd_no,color\nM1AG702,Natural\n", encoding="utf-8")

    rows = list(read_table_rows(path, SourceParseOptions(header_row=2)))

    assert rows == [{"md_no": "M1AG702", "color": "Natural"}]


def test_read_table_rows_counts_csv_records_not_physical_lines(tmp_path: Path):
    """带引号的字段里可以有换行，那种记录横跨多个物理行。header_row 数的是
    记录——用户在 Excel 里看到的行号也是记录号。按物理行数会错位。"""
    path = tmp_path / "multiline.csv"
    path.write_text('"标题\n第二行",x\nmd_no,color\nM1AG702,Natural\n', encoding="utf-8")

    rows = list(read_table_rows(path, SourceParseOptions(header_row=2)))

    assert rows == [{"md_no": "M1AG702", "color": "Natural"}]


def test_read_table_rows_honours_header_row_for_xls(tmp_path: Path):
    path = tmp_path / "layered.xls"
    _write_xls(path, [["Product Information", ""], ["md_no", "color"], ["M1AG702", "Natural"]])

    rows = list(read_table_rows(path, SourceParseOptions(header_row=2)))

    assert rows == [{"md_no": "M1AG702", "color": "Natural"}]


def test_read_table_rows_yields_nothing_when_header_row_is_past_the_end(tmp_path: Path):
    path = tmp_path / "short.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")

    assert list(read_table_rows(path, SourceParseOptions(header_row=9))) == []


def test_read_table_rows_rejects_sheet_on_csv(tmp_path: Path):
    """CSV 没有工作表。安静忽略会让用户以为自己选中了某张表。"""
    path = tmp_path / "data.csv"
    path.write_text("a\n1\n", encoding="utf-8")

    with pytest.raises(RowProcessingError, match="工作表"):
        list(read_table_rows(path, SourceParseOptions(sheet="Master")))


def test_source_parse_options_rejects_header_row_below_one():
    """1-based 跟 Excel 的行号对齐。允许 0 会让两套下标混在一起。"""
    with pytest.raises(InvalidSourceParseOptionsError, match="header_row"):
        SourceParseOptions(header_row=0)


def test_source_parse_options_rejects_first_data_row_not_after_header():
    with pytest.raises(InvalidSourceParseOptionsError, match="first_data_row"):
        SourceParseOptions(header_row=3, first_data_row=3)
```

- [ ] **Step 2: 跑测试确认它失败**

```
python -u -m pytest tests/graphrag/test_etl_staging.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'app.graphrag.source_parse_options'`

- [ ] **Step 3: 写 `app/graphrag/source_parse_options.py`**

```python
"""staging 层的解析选项：一张表该怎么读。

这里回答的是"这张表怎么读"，不是"读出来的列怎么映射到本体"——后者是
projection 层和本体的事。今天这两件事挤在同一步里，正是 MUJI 那张
113 列、四层表头的 SKU 主数据表暴露出来的问题。

见 docs/superpowers/specs/2026-09-15-source-parse-options-design.md。
"""

from __future__ import annotations

from dataclasses import dataclass


class InvalidSourceParseOptionsError(Exception):
    """解析选项自身不合法——表头行号小于 1、首数据行不在表头之后等。"""


@dataclass(frozen=True)
class SourceParseOptions:
    """缺省值必须等于"本选项引入之前的行为"：第一个工作表、第一行表头。

    存量的映射里没有 sources 段，会按缺省解释；任何一个缺省值变了，
    所有存量配置的行为都会跟着变，而没有人会收到通知。
    """

    #: 工作表名或 0-based 序号；None = 第一个。csv/tsv 必须是 None。
    #: 两种都收：名字可读、能自解释，但会被重命名；序号稳定、但看不出是哪张。
    sheet: str | int | None = None
    #: 1-based，跟 Excel 的行号对齐。用户是对着 Excel 看的。
    header_row: int = 1
    #: 1-based；None = header_row + 1。
    #:
    #: 独立字段而不是算出来的，是因为表头和数据之间可能夹着说明行——MUJI
    #: 那张表第 5 行是"Character Limit"，用户若选第 3 行的英文名当表头，
    #: 中间三行都得跳过。
    first_data_row: int | None = None

    def __post_init__(self) -> None:
        if self.header_row < 1:
            raise InvalidSourceParseOptionsError(
                f"header_row 必须从 1 开始（跟 Excel 行号一致），收到 {self.header_row}"
            )
        if self.first_data_row is not None and self.first_data_row <= self.header_row:
            raise InvalidSourceParseOptionsError(
                f"first_data_row（{self.first_data_row}）必须大于 header_row（{self.header_row}）"
            )
        if isinstance(self.sheet, int) and self.sheet < 0:
            raise InvalidSourceParseOptionsError(f"sheet 序号不能是负数，收到 {self.sheet}")

    @property
    def resolved_first_data_row(self) -> int:
        """紧跟表头是绝大多数表的形状，所以 first_data_row 缺省可省。"""
        return self.first_data_row if self.first_data_row is not None else self.header_row + 1
```

- [ ] **Step 4: 改 `etl_staging.py`**

顶部 import 增加：

```python
from app.graphrag.source_parse_options import SourceParseOptions
```

`_read_delimited_rows` 整体替换为：

```python
def _read_delimited_rows(
    path: Path, *, delimiter: str, options: SourceParseOptions
) -> Iterator[dict[str, str]]:
    """逐行流式产出 CSV/TSV 源文件的行——设计文档第 6.4 节给出的真实规模
    是"MUJI 一张 SKU 表 18 万+ 行"。注意：编码探测阶段（见
    _detect_text_encoding）会把整个文件读一遍原始字节做 decode 测试，
    这一步不是流式的；探测完成后的逐行处理本身才是流式、不整份文本
    常驻内存。跳到 header_row 同样是流式的——只是把前面的记录读过去丢掉，
    不会把它们攒起来。"""
    encoding = _detect_text_encoding(path)
    with path.open(encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        # header_row 数的是 CSV **记录**，不是物理行：带引号的字段里可以有
        # 换行，那种记录横跨多个物理行。用户在 Excel 里看到的行号也是记录号，
        # 按物理行数会跟他看到的对不上。
        header: list[str] | None = None
        for record_number, row in enumerate(reader, start=1):
            if record_number == options.header_row:
                header = deduplicate_header(row)
                break
        if header is None:
            # 表头行号超出文件长度。产出空序列，跟"空文件"同一个终态。
            return
        first_data_row = options.resolved_first_data_row
        for record_number, row in enumerate(reader, start=options.header_row + 1):
            if record_number < first_data_row:
                continue
            # csv.DictReader 会跳过空行，这里手工复现同样的行为。
            if not row:
                continue
            # 值比表头少时补 None，跟 DictReader 的 restval 默认值一致；
            # 多出来的尾部值没有列名可挂，丢掉——DictReader 把它们塞进
            # key=None 的列表里，下游同样一次都没读过。
            values = {name: row[i] if i < len(row) else None for i, name in enumerate(header)}
            yield values
```

新增 sheet 选择辅助函数（放在 `_read_xlsx_rows` 之前）：

```python
def _select_xlsx_sheet(workbook, sheet: str | int | None):
    """选不中就报错，绝不回落到第一张表——回落会让用户拿到一份完全不相干
    的数据，而且不报错。"""
    if sheet is None:
        return workbook.worksheets[0]
    if isinstance(sheet, int):
        if sheet >= len(workbook.worksheets):
            raise RowProcessingError(
                f"工作表序号 {sheet} 超出范围：这个文件只有 {len(workbook.worksheets)} 张表"
            )
        return workbook.worksheets[sheet]
    if sheet not in workbook.sheetnames:
        raise RowProcessingError(
            f"找不到名为 {sheet!r} 的工作表，这个文件里有：{workbook.sheetnames}"
        )
    return workbook[sheet]
```

`_read_xlsx_rows` 整体替换为：

```python
def _read_xlsx_rows(path: Path, *, options: SourceParseOptions) -> Iterator[dict[str, str]]:
    """流式读取 xlsx 的指定工作表与表头行。read_only=True 让 openpyxl 用
    懒加载模式逐行产出，不把整个工作表读进内存，跟 CSV 路径同一个"18 万+
    行不能爆内存"的约束。data_only=True 拿单元格公式算出来的值，不拿公式
    字符串本身。

    工作表和表头行曾经写死成"第一个 sheet、第一行"，见
    docs/superpowers/specs/2026-09-15-source-parse-options-design.md
    ——那个决策在这里被推翻。"""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = _select_xlsx_sheet(workbook, options.sheet)
        rows_iter = worksheet.iter_rows(values_only=True)
        header_cells: tuple | None = None
        for row_number, row in enumerate(rows_iter, start=1):
            if row_number == options.header_row:
                header_cells = row
                break
        if header_cells is None:
            return
        header = deduplicate_header(
            [str(cell).strip() if cell is not None else "" for cell in header_cells]
        )
        first_data_row = options.resolved_first_data_row
        for row_number, row in enumerate(rows_iter, start=options.header_row + 1):
            if row_number < first_data_row:
                continue
            values = {
                header[i]: convert_excel_cell_to_string(row[i] if i < len(row) else None)
                for i in range(len(header))
            }
            # openpyxl 的 read_only 迭代会按工作表"已用范围"补齐行数，哪怕
            # 某一行早就被清空也会产出全空的幽灵行（常见于手工编辑过的
            # Excel 导出文件）。跳过全空行，避免这些幽灵行被当成"缺列"的
            # 脏数据行计入 skipped_rows，也让这条路径跟 CSV 侧对空行的
            # 处理保持一致。
            if not any(values.values()):
                continue
            yield values
    finally:
        workbook.close()
```

`_read_xls_rows` 前新增 sheet 选择，并整体替换读取器：

```python
def _select_xls_sheet(workbook: "xlrd.book.Book", sheet: str | int | None):
    """理由同 _select_xlsx_sheet：选不中就报错，不回落。"""
    if sheet is None:
        return workbook.sheet_by_index(0)
    if isinstance(sheet, int):
        if sheet >= workbook.nsheets:
            raise RowProcessingError(
                f"工作表序号 {sheet} 超出范围：这个文件只有 {workbook.nsheets} 张表"
            )
        return workbook.sheet_by_index(sheet)
    if sheet not in workbook.sheet_names():
        raise RowProcessingError(
            f"找不到名为 {sheet!r} 的工作表，这个文件里有：{workbook.sheet_names()}"
        )
    return workbook.sheet_by_name(sheet)


def _read_xls_rows(path: Path, *, options: SourceParseOptions) -> Iterator[dict[str, str]]:
    """读取旧版二进制 xls 的指定工作表与表头行。xlrd 没有 openpyxl 那种
    懒加载流式模式，会把整个工作表读进内存——xls 是被淘汰的旧格式，体量
    通常不大，这里不为了流式特意做额外处理。"""
    workbook = xlrd.open_workbook(str(path))
    worksheet = _select_xls_sheet(workbook, options.sheet)
    header_idx = options.header_row - 1
    if worksheet.nrows <= header_idx:
        return
    header = deduplicate_header(
        [str(worksheet.cell_value(header_idx, col)).strip() for col in range(worksheet.ncols)]
    )
    for row_idx in range(options.resolved_first_data_row - 1, worksheet.nrows):
        values = {
            header[col_idx]: convert_excel_cell_to_string(
                _xlrd_cell_to_python_value(worksheet.cell(row_idx, col_idx), workbook.datemode)
            )
            for col_idx in range(len(header))
        }
        # 跟 _read_xlsx_rows 同样的理由：跳过全空行。
        if not any(values.values()):
            continue
        yield values
```

`read_table_rows` 整体替换为：

```python
def read_table_rows(
    path: Path, options: SourceParseOptions | None = None
) -> Iterator[dict[str, str]]:
    """按扩展名分流到对应的行读取器，统一产出 dict[str, str]。

    这是 ETL 三层管道的第一层（staging）的唯一入口：解析 + 类型归一，
    不认识 node_key、不认识本体，只把各种格式的表统一成行序列。见
    docs/superpowers/specs/2026-08-30-etl-layered-pipeline-design.md。

    options 省略时的行为跟解析选项引入之前完全一致（第一个工作表、第一行
    表头）——存量映射里没有 sources 段，全靠这条保持不变。
    """
    opts = options if options is not None else SourceParseOptions()
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        if opts.sheet is not None:
            # 安静忽略会让用户以为自己选中了某张表。
            raise RowProcessingError(
                f"{suffix} 文件没有工作表的概念，不能指定 sheet：{path.name}"
            )
        delimiter = "," if suffix == ".csv" else "\t"
        yield from _read_delimited_rows(path, delimiter=delimiter, options=opts)
    elif suffix == ".xlsx":
        yield from _read_xlsx_rows(path, options=opts)
    elif suffix == ".xls":
        yield from _read_xls_rows(path, options=opts)
    else:
        raise RowProcessingError(f"不支持的数据文件类型: {suffix!r}（{path.name}）")
```

顺手把 `app/graphrag/schema_etl_row_processing.py:91` 和 `app/api/admin_schema_etl_routes.py:79` 里指向 `2026-08-21-schema-etl-multi-format-upload.md` 的引用改成指向本 spec——那份文档已在 `354cabe` 删除，现在是失效链接。

- [ ] **Step 5: 跑测试确认通过**

```
python -u -m pytest tests/graphrag/test_etl_staging.py -q
```
Expected: PASS，且**原有的 17 条测试一条都没改过**——这是"缺省=今天行为"的判据。

- [ ] **Step 6: 变异测试**

逐个应用、跑测试、确认变红、改回：

| 变异 | 期望变红的测试 |
|---|---|
| `_select_xlsx_sheet` 找不到时 `return workbook.worksheets[0]` 而不是报错 | `test_read_table_rows_reports_a_missing_sheet_by_name` |
| `resolved_first_data_row` 永远返回 `header_row + 1` | `test_read_table_rows_skips_the_notes_rows_between_header_and_data` |
| csv 分支去掉 `if opts.sheet is not None` 的报错 | `test_read_table_rows_rejects_sheet_on_csv` |
| `__post_init__` 去掉 `header_row < 1` 检查 | `test_source_parse_options_rejects_header_row_below_one` |
| csv 读表头改成按物理行计数（先在 `handle` 上 `next()` N 次再交给 csv.reader） | `test_read_table_rows_counts_csv_records_not_physical_lines` |

- [ ] **Step 7: 跑全量后端测试**

```
python -u -m pytest tests/ -q
```
Expected: 全绿（基线 2468 passed）

- [ ] **Step 8: 提交**

```bash
git add app/graphrag/source_parse_options.py app/graphrag/etl_staging.py \
        app/graphrag/schema_etl_row_processing.py app/api/admin_schema_etl_routes.py \
        tests/graphrag/test_etl_staging.py
git commit -m "feat(etl): staging 支持选 sheet、选表头行、选首数据行"
```

---

### Task 2: 配置 YAML 的 `sources:` 段

**Files:**
- Modify: `app/graphrag/schema_etl_config.py:41-45`（`SchemaETLConfig`）、`:120-129`（`parse_schema_etl_config`）、`:132-185`（`summarize_schema_etl_config`）
- Modify: `app/graphrag/etl_projection.py:168`、`:209`、`:245`
- Test: `tests/graphrag/test_schema_etl_config.py`、`tests/graphrag/test_etl_projection.py`

**Interfaces:**
- Consumes: `SourceParseOptions`、`read_table_rows(path, options)`（Task 1）
- Produces: `SchemaETLConfig.sources: dict[str, SourceParseOptions]`（键是 `source_file` 文件名）；摘要新增 `"sources"` 键

- [ ] **Step 1: 写失败的测试**

追加到 `tests/graphrag/test_schema_etl_config.py`：

```python
from app.graphrag.source_parse_options import SourceParseOptions


def test_parse_schema_etl_config_reads_the_sources_section():
    config = parse_schema_etl_config(
        """
tenant_id: muji
sources:
  - file: CN_001_SKU_MASTER_121.xls
    sheet: Master
    header_row: 6
    first_data_row: 7
entities: []
relations: []
"""
    )

    assert config.sources == {
        "CN_001_SKU_MASTER_121.xls": SourceParseOptions(
            sheet="Master", header_row=6, first_data_row=7
        )
    }


def test_parse_schema_etl_config_without_sources_section_yields_no_options():
    """存量配置没有这一段。缺省解释必须等于"按老行为读"，不做迁移。"""
    config = parse_schema_etl_config("tenant_id: muji\nentities: []\nrelations: []\n")

    assert config.sources == {}


def test_parse_schema_etl_config_rejects_a_sources_entry_without_file():
    """没有 file 就不知道这份选项管的是哪张表，静默丢掉等于选项没生效。"""
    with pytest.raises(InvalidSchemaETLConfigError, match="file"):
        parse_schema_etl_config(
            "tenant_id: muji\nsources:\n  - header_row: 6\nentities: []\nrelations: []\n"
        )


def test_parse_schema_etl_config_rejects_two_entries_for_the_same_file():
    """同一个文件两份选项，谁生效取决于 dict 覆盖顺序——那是掷骰子。"""
    with pytest.raises(InvalidSchemaETLConfigError, match="a.xls"):
        parse_schema_etl_config(
            "tenant_id: muji\nsources:\n  - file: a.xls\n    header_row: 2\n"
            "  - file: a.xls\n    header_row: 3\nentities: []\nrelations: []\n"
        )


def test_parse_schema_etl_config_surfaces_invalid_options_as_config_errors():
    """用户看到的是一份配置文件，不该收到一个来自内部数据类的异常类型。"""
    with pytest.raises(InvalidSchemaETLConfigError, match="header_row"):
        parse_schema_etl_config(
            "tenant_id: muji\nsources:\n  - file: a.xls\n    header_row: 0\n"
            "entities: []\nrelations: []\n"
        )


def test_summarize_schema_etl_config_includes_sources():
    """摘要是表格导入页回填表单的唯一来源。不带解析选项的话，用户重开页面
    就会看到"表头在第 1 行"，跟他上次存的不一样，而且没有任何提示。"""
    config = parse_schema_etl_config(
        "tenant_id: muji\nsources:\n  - file: a.xls\n    sheet: Master\n    header_row: 6\n"
        "entities: []\nrelations: []\n"
    )

    summary = summarize_schema_etl_config(config)

    assert summary["sources"] == [
        {"file": "a.xls", "sheet": "Master", "header_row": 6, "first_data_row": None}
    ]
```

追加到 `tests/graphrag/test_etl_projection.py`：

```python
from app.graphrag.source_parse_options import SourceParseOptions


async def test_project_entity_rows_reads_the_source_with_its_parse_options(tmp_path: Path):
    """选项只存不使用的话，界面上一切正常，跑批仍然读第一行表头——这正是
    这个功能要修的 bug 换了个地方复发。"""
    _write_csv(
        tmp_path / "customers.csv",
        [
            "本表仅供内部使用,,",
            "name,zip,city",
            "张三,100,北京",
        ],
    )
    conn = await _conn()

    rows = [
        row
        async for row in project_entity_rows(
            conn,
            tenant_id="demo",
            mapping=_mapping(),
            extra_field_specs={},
            data_dir=tmp_path,
            parse_options=SourceParseOptions(header_row=2),
        )
    ]

    assert [r.node_key for r in rows] == ["客户:张三|100"]


async def test_project_entity_rows_numbers_rows_from_the_real_first_data_row(tmp_path: Path):
    """报错信息里的"第 N 行"要跟用户在 Excel 里看到的行号对上。表头在第 2
    行时，第一条数据是第 3 行，不是第 2 行。"""
    _write_csv(
        tmp_path / "customers.csv",
        [
            "本表仅供内部使用,,",
            "name,zip,city",
            "张三,,北京",
        ],
    )
    conn = await _conn()

    results = [
        row
        async for row in project_entity_rows(
            conn,
            tenant_id="demo",
            mapping=_mapping(),
            extra_field_specs={},
            data_dir=tmp_path,
            parse_options=SourceParseOptions(header_row=2),
        )
    ]

    failures = [r for r in results if isinstance(r, RowFailure)]
    assert [f.row_number for f in failures] == [3]


async def test_scan_entity_node_keys_reads_the_source_with_its_parse_options(tmp_path: Path):
    """两遍扫描必须用同一份选项。第一遍按第 1 行读、第二遍按第 2 行读的话，
    预检查到的键跟真正写入的键不是同一批——预检就等于没做。"""
    _write_csv(
        tmp_path / "customers.csv",
        [
            "本表仅供内部使用,,",
            "name,zip,city",
            "张三,100,北京",
            "张三,100,上海",
        ],
    )
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn,
        tenant_id="demo",
        mapping=_mapping(),
        extra_field_specs={},
        data_dir=tmp_path,
        parse_options=SourceParseOptions(header_row=2),
    )

    # 同一个 node_key 两行、city 不同 —— 这是值冲突，必须被检出来。
    assert result.conflicts
```

**注意：** 上面用到的 `RowFailure.row_number` 字段名以该模块实际定义为准；若字段叫别的名字，按实际的改，不要改测试意图。

- [ ] **Step 2: 跑测试确认失败**

```
python -u -m pytest tests/graphrag/test_schema_etl_config.py tests/graphrag/test_etl_projection.py -q
```
Expected: FAIL，`AttributeError: 'SchemaETLConfig' object has no attribute 'sources'`

- [ ] **Step 3: 改 `schema_etl_config.py`**

顶部 import 增加，并把 `from dataclasses import dataclass` 改成 `from dataclasses import dataclass, field`：

```python
from app.graphrag.source_parse_options import (
    InvalidSourceParseOptionsError,
    SourceParseOptions,
)
```

`SchemaETLConfig` 改为：

```python
@dataclass(frozen=True)
class SchemaETLConfig:
    tenant_id: str
    entities: list[EntityMapping]
    relations: list[RelationMapping]
    #: 文件名 → 这张表怎么读。没有条目的文件按 SourceParseOptions() 的缺省读。
    #: 属于 staging 层，不进 entities/relations，也不进本体。
    sources: dict[str, SourceParseOptions] = field(default_factory=dict)
```

新增解析函数：

```python
def _parse_sources(raw_list: list) -> dict[str, SourceParseOptions]:
    sources: dict[str, SourceParseOptions] = {}
    for raw in raw_list:
        if not isinstance(raw, dict) or not raw.get("file"):
            raise InvalidSchemaETLConfigError(
                f"sources 的每一条都必须有 file（这份选项管的是哪张表），收到: {raw!r}"
            )
        file_name = raw["file"]
        if file_name in sources:
            # 谁生效取决于 dict 的覆盖顺序，那是掷骰子。
            raise InvalidSchemaETLConfigError(f"sources 里 {file_name!r} 出现了不止一次")
        try:
            sources[file_name] = SourceParseOptions(
                sheet=raw.get("sheet"),
                header_row=raw.get("header_row", 1),
                first_data_row=raw.get("first_data_row"),
            )
        except InvalidSourceParseOptionsError as e:
            # 用户看到的是一份配置文件，不该收到一个来自内部数据类的异常类型。
            raise InvalidSchemaETLConfigError(f"{file_name} 的解析选项不合法：{e}") from e
    return sources
```

`parse_schema_etl_config` 的 `return` 增加一行 `sources=_parse_sources(data.get("sources") or []),`。

`summarize_schema_etl_config` 的 `return` 改为：

```python
    sources = [
        {
            "file": file_name,
            "sheet": opts.sheet,
            "header_row": opts.header_row,
            "first_data_row": opts.first_data_row,
        }
        for file_name, opts in config.sources.items()
    ]
    return {"entities": entities, "relations": relations, "sources": sources}
```

并在该函数 docstring 末尾补一段：

```
    ``sources`` 是 staging 层的解析选项（这张表怎么读），跟 entities/relations
    （读出来的列怎么映射到本体）是两回事。摘要必须带上它——表格导入页靠摘要
    回填表单，不带的话用户重开页面会看到"表头在第 1 行"，跟他上次存的不一样，
    而且没有任何提示。
```

- [ ] **Step 4: 改 `etl_projection.py` 的三处调用点**

`scan_entity_node_keys`（`:134`）、`project_entity_rows`（`:196`）、`project_relation_rows`（`:227`）三个函数都是纯关键字参数、**拿不到 `SchemaETLConfig`**。各加一个关键字参数，由调用方从 `config.sources` 里查好了传进来：

```python
    parse_options: SourceParseOptions | None = None,
```

**不要**把 `SchemaETLConfig` 整个传进来，也**不要**在函数内部重新加载配置文件——这一层的职责是"按给定的选项读这一个文件"，认识整份配置只会让它更难测。

三处循环都从

```python
    for row_number, row in enumerate(read_table_rows(data_dir / mapping.source_file), start=2):
```

改为

```python
    # start=2 是老写法留下的：它假定表头永远在第 1 行、数据从第 2 行开始。
    # 解析选项引入之后，行号必须从这张表真正的首数据行算起，否则报错信息里
    # 的"第 N 行"会跟用户在 Excel 里看到的对不上。
    options = parse_options if parse_options is not None else SourceParseOptions()
    for row_number, row in enumerate(
        read_table_rows(data_dir / mapping.source_file, options),
        start=options.resolved_first_data_row,
    ):
```

调用方在 `app/graphrag/schema_etl.py` 里（`grep -n "project_entity_rows\|scan_entity_node_keys\|project_relation_rows" app/graphrag/schema_etl.py` 找到全部调用点），每处补上：

```python
        parse_options=config.sources.get(mapping.source_file),
```

关系侧用 `mapping.source_file` 同样取——关系映射也有自己的 `source_file`。

**两遍扫描必须用同一份选项**：`scan_entity_node_keys` 和 `project_entity_rows` 读的是同一个文件，第一遍按第 1 行读、第二遍按第 2 行读的话，预检查到的键跟真正写入的键不是同一批，预检就等于没做。

- [ ] **Step 5: 跑测试确认通过**

```
python -u -m pytest tests/graphrag/test_schema_etl_config.py tests/graphrag/test_etl_projection.py -q
```

- [ ] **Step 6: 变异测试**

| 变异 | 期望变红 |
|---|---|
| `_parse_sources` 去掉重复 file 的检查 | `test_parse_schema_etl_config_rejects_two_entries_for_the_same_file` |
| `summarize` 不输出 `sources` | `test_summarize_schema_etl_config_includes_sources` |
| projection 调用点不传 `options` | `test_projection_reads_the_source_with_its_parse_options` |
| `_parse_sources` 捕获异常后 `pass` 掉 | `test_parse_schema_etl_config_surfaces_invalid_options_as_config_errors` |

- [ ] **Step 7: 提交**

```bash
git add app/graphrag/schema_etl_config.py app/graphrag/etl_projection.py \
        tests/graphrag/test_schema_etl_config.py tests/graphrag/test_etl_projection.py
git commit -m "feat(etl): 配置 YAML 支持 sources 段，解析选项跟映射一起存"
```

---

### Task 3: 跑批时前后端列名对账

**Files:**
- Modify: `app/api/admin_schema_etl_routes.py:258-381`（`start_schema_etl_run`）
- Test: `tests/api/test_admin_schema_etl_routes.py`

**Interfaces:**
- Consumes: `SchemaETLConfig.sources`（Task 2）、`read_table_rows`（Task 1）
- Produces: `POST /admin/schema-etl/runs` 新增可选表单字段 `client_columns`（JSON 字符串，`{文件名: [列名...]}`）

- [ ] **Step 1: 写失败的测试**

按该文件既有的 client/fixture 风格写：

```python
async def test_start_run_rejects_a_column_list_that_disagrees_with_the_backend(client):
    """前端本地解析、后端跑批解析，两份规则会悄悄分叉——9c71cf9 已经让它们
    分叉过一次（后端给重名列加了后缀，前端没有）。分叉时按后端的悄悄跑，
    用户会拿到一份跟他在界面上看到的不一样的映射结果，而且没有任何提示。"""
    response = await client.post(
        "/admin/schema-etl/runs?tenant_id=demo",
        files={"data_files": ("dup.csv", b"Color,Color\na,b\n", "text/csv")},
        data={"client_columns": json.dumps({"dup.csv": ["Color", "Color"]})},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "dup.csv" in detail
    assert "Color (2)" in detail  # 后端算出来的
    assert "未写入任何数据" in detail


async def test_start_run_accepts_a_matching_column_list(client):
    response = await client.post(
        "/admin/schema-etl/runs?tenant_id=demo",
        files={"data_files": ("dup.csv", b"Color,Color\na,b\n", "text/csv")},
        data={"client_columns": json.dumps({"dup.csv": ["Color", "Color (2)"]})},
    )

    assert response.status_code == 200


async def test_start_run_rejects_the_same_columns_in_a_different_order(client):
    """顺序决定哪一列对应哪个位置。乱序必须算不一致，不能按集合比。"""
    response = await client.post(
        "/admin/schema-etl/runs?tenant_id=demo",
        files={"data_files": ("ab.csv", b"a,b\n1,2\n", "text/csv")},
        data={"client_columns": json.dumps({"ab.csv": ["b", "a"]})},
    )

    assert response.status_code == 400


async def test_start_run_without_client_columns_still_works(client):
    """老前端、curl、以及重跑历史 run 都不会带这个字段。缺省必须是"不对账"，
    不能是"对账失败"。"""
    response = await client.post(
        "/admin/schema-etl/runs?tenant_id=demo",
        files={"data_files": ("ok.csv", b"a,b\n1,2\n", "text/csv")},
    )

    assert response.status_code == 200


async def test_start_run_ignores_client_columns_for_files_it_did_not_receive(client):
    """前端可能带上它本地加过、后来又移除的文件。多出来的条目不该让跑批失败。"""
    response = await client.post(
        "/admin/schema-etl/runs?tenant_id=demo",
        files={"data_files": ("ok.csv", b"a,b\n1,2\n", "text/csv")},
        data={"client_columns": json.dumps({"ok.csv": ["a", "b"], "gone.csv": ["x"]})},
    )

    assert response.status_code == 200
```

- [ ] **Step 2: 跑测试确认失败**

```
python -u -m pytest tests/api/test_admin_schema_etl_routes.py -q
```
Expected: 第一条 FAIL（返回 200 而不是 400）

- [ ] **Step 3: 实现对账**

在 `start_schema_etl_run` 的签名里加 `client_columns: str | None = Form(None)`，并在**已经把 config 和数据文件落盘之后、`background_tasks.add_task` 之前**插入：

```python
    if client_columns is not None:
        try:
            declared: dict[str, list[str]] = json.loads(client_columns)
        except json.JSONDecodeError as e:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400, detail=f"client_columns 不是合法的 JSON：{e}"
            ) from e
        parsed_config = parse_schema_etl_config(
            config_path.read_text(encoding="utf-8"), origin=str(config_path)
        )
        mismatches = _find_column_mismatches(run_dir, parsed_config, declared)
        if mismatches:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=_format_column_mismatch(mismatches))
```

**注意：** 对账必须留在这个请求里，**不要**挪进后台任务——挪进去的话用户点完按钮看到的是"已开始"，失败要过一会儿才在运行列表里出现。

新增两个模块级函数：

```python
def _find_column_mismatches(
    run_dir: Path,
    config: SchemaETLConfig,
    declared: dict[str, list[str]],
) -> list[tuple[str, list[str], list[str]]]:
    """按后端自己的规则解析每个文件的表头，跟前端声明的比对。

    只读表头一行——`read_table_rows` 是生成器，取一行就停，18 万行的表也
    不会被整份读进来。

    前端声明了、但这次没上传的文件跳过：用户可能在界面上加过又移除，多出来
    的条目不该让跑批失败。
    """
    mismatches: list[tuple[str, list[str], list[str]]] = []
    for file_name, client_side in declared.items():
        path = run_dir / file_name
        if not path.exists():
            continue
        options = config.sources.get(file_name) or SourceParseOptions()
        first = next(iter(read_table_rows(path, options)), None)
        server_side = list(first.keys()) if first is not None else []
        # 按顺序逐项比，不比集合：顺序决定哪一列对应哪个位置。
        if server_side != list(client_side):
            mismatches.append((file_name, list(client_side), server_side))
    return mismatches


def _format_column_mismatch(mismatches: list[tuple[str, list[str], list[str]]]) -> str:
    """报错必须把两边的列名都列出来。只说"不一致"的话，用户既不知道是哪一列，
    也无从判断该改哪边。"""
    lines = ["页面上看到的列名跟服务端解析出来的对不上，本次未写入任何数据。"]
    for file_name, client_side, server_side in mismatches:
        only_client = [c for c in client_side if c not in server_side]
        only_server = [c for c in server_side if c not in client_side]
        lines.append(f"{file_name}：页面 {len(client_side)} 列，服务端 {len(server_side)} 列。")
        if only_client:
            lines.append(f"  只在页面上有：{only_client}")
        if only_server:
            lines.append(f"  只在服务端有：{only_server}")
    lines.append(
        "常见原因：这张表的解析设置（工作表/表头行）在页面和配置里不一致，"
        "或者页面上的文件跟上传的不是同一份。"
    )
    return "\n".join(lines)
```

顶部按需 import `json`、`SourceParseOptions`、`read_table_rows`、`parse_schema_etl_config`。

- [ ] **Step 4: 跑测试确认通过**

```
python -u -m pytest tests/api/test_admin_schema_etl_routes.py -q
```

- [ ] **Step 5: 变异测试**

| 变异 | 期望变红 |
|---|---|
| 比对改成 `set(server_side) != set(client_side)` | `test_start_run_rejects_the_same_columns_in_a_different_order` |
| `client_columns is None` 时也走对账（当成空 dict） | `test_start_run_without_client_columns_still_works` |
| 不存在的文件也参与对账 | `test_start_run_ignores_client_columns_for_files_it_did_not_receive` |
| `_format_column_mismatch` 只输出"不一致"三个字 | 第一条测试里 `"Color (2)" in detail` |

- [ ] **Step 6: 提交**

```bash
git add app/api/admin_schema_etl_routes.py tests/api/test_admin_schema_etl_routes.py
git commit -m "feat(etl): 跑批前跟前端对账列名，不一致整体失败"
```

---

### Task 4: 去重规则的共享判据

**Files:**
- Create: `fixtures/header-dedup-cases.json`
- Modify: `tests/graphrag/test_etl_staging.py`
- Create: `frontend/src/admin/schemaEtlConfigBuilder/headerDedupCases.test.ts`

**Interfaces:**
- Consumes: `deduplicate_header`（后端既有）
- Produces: `fixtures/header-dedup-cases.json`，形状 `{"cases": [{"name": str, "input": [str], "expected": [str]}]}`

本任务的前端测试文件先写好，但它依赖 Task 5 的 `sourceParser.ts`，**允许它红着进 Task 5**——这是本计划里唯一一处允许跨任务红的地方。理由：fixture 必须先于两边的实现存在，否则两边会各自先长出自己的用例表，那正是要防的东西。

- [ ] **Step 1: 建 fixture**

`fixtures/header-dedup-cases.json`：

```json
{
  "_comment": "重名列去重规则的共享判据。后端 tests/graphrag/test_etl_staging.py 和前端 frontend/src/admin/schemaEtlConfigBuilder/headerDedupCases.test.ts 各读一遍，跑同一组用例。语言不同没法共享代码，但可以共享判据——这是唯一能真正防住两边规则分叉的手段。改这个文件等于同时改两边的契约。",
  "cases": [
    {"name": "没有重名时一个后缀都不加", "input": ["Color", "Size"], "expected": ["Color", "Size"]},
    {"name": "两列同名", "input": ["Color", "Color"], "expected": ["Color", "Color (2)"]},
    {"name": "三列同名按出现顺序递增", "input": ["Depth", "Depth", "Depth"], "expected": ["Depth", "Depth (2)", "Depth (3)"]},
    {"name": "表头里本来就有带后缀的名字", "input": ["Color", "Color (2)", "Color"], "expected": ["Color", "Color (2)", "Color (3)"]},
    {"name": "空列名彼此也算重名", "input": ["name", "", ""], "expected": ["name", "", " (2)"]},
    {"name": "全空表头", "input": ["", "", ""], "expected": ["", " (2)", " (3)"]},
    {"name": "重名不相邻", "input": ["a", "b", "a"], "expected": ["a", "b", "a (2)"]},
    {"name": "空表头列表", "input": [], "expected": []}
  ]
}
```

- [ ] **Step 2: 后端读 fixture 的测试**

追加到 `tests/graphrag/test_etl_staging.py`：

```python
import json as _json

from app.graphrag.etl_staging import deduplicate_header

_DEDUP_CASES_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "header-dedup-cases.json"


def _load_dedup_cases() -> list[dict]:
    return _json.loads(_DEDUP_CASES_PATH.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _load_dedup_cases(), ids=lambda c: c["name"])
def test_deduplicate_header_matches_the_shared_cases(case: dict):
    assert deduplicate_header(case["input"]) == case["expected"]


def test_shared_dedup_case_file_is_not_silently_empty():
    """fixture 少了几条或者被清空，两边都会"全绿"——而全绿的原因是没跑用例。
    这条断言是对那种静默失效的唯一防线。"""
    assert len(_load_dedup_cases()) >= 8
```

- [ ] **Step 3: 前端读同一份 fixture 的测试**

`frontend/src/admin/schemaEtlConfigBuilder/headerDedupCases.test.ts`：

```ts
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { deduplicateHeader } from './sourceParser'

// 跟后端 tests/graphrag/test_etl_staging.py 读的是同一个文件。语言不同没法
// 共享代码，但可以共享判据——这是唯一能真正防住两边去重规则分叉的手段，
// 而不是靠两边各自写注释提醒对方。
const CASES_PATH = resolve(__dirname, '../../../../fixtures/header-dedup-cases.json')
const cases = JSON.parse(readFileSync(CASES_PATH, 'utf-8')).cases as {
  name: string
  input: string[]
  expected: string[]
}[]

describe('deduplicateHeader 与后端同源', () => {
  it.each(cases)('$name', ({ input, expected }) => {
    expect(deduplicateHeader(input)).toEqual(expected)
  })

  it('用例数跟后端的下限一致', () => {
    // fixture 被清空时 it.each 会一条都不跑，测试文件依然"全绿"。
    expect(cases.length).toBeGreaterThanOrEqual(8)
  })
})
```

- [ ] **Step 4: 跑后端那一半**

```
python -u -m pytest tests/graphrag/test_etl_staging.py -q
```
Expected: PASS（前端那一半此时还红着，Task 5 修好）

- [ ] **Step 5: 变异测试**

把 fixture 里"表头里本来就有带后缀的名字"那条的 `expected` 改成 `["Color", "Color (2)", "Color (2)"]`，确认后端测试变红——证明测试真的在读这个文件，而不是读了个缓存或者根本没跑。改回。

- [ ] **Step 6: 提交**

```bash
git add fixtures/header-dedup-cases.json tests/graphrag/test_etl_staging.py \
        frontend/src/admin/schemaEtlConfigBuilder/headerDedupCases.test.ts
git commit -m "test(etl): 重名列去重规则改由前后端共享的判据锁住"
```

---

### Task 5: 前端解析器收敛成 `sourceParser.ts`

**Files:**
- Create: `frontend/src/admin/schemaEtlConfigBuilder/sourceParser.ts`
- Create: `frontend/src/admin/schemaEtlConfigBuilder/sourceParser.test.ts`
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/tableHeader.ts`（整个文件缩成薄封装）
- Modify: `frontend/src/admin/guidedOntology/columnStats.ts:263-283`（`scanTableFile`）、`:284-303`（`scanPairs`）、`:313-322`（私有 `readTableRows`）、`:324-357`（`readExcelRows`）、`:370-400`（`readDelimitedRows`）

**Interfaces:**
- Consumes: `fixtures/header-dedup-cases.json`（Task 4）
- Produces:
  - `export interface SourceParseOptions { sheet?: string | number; headerRow?: number; firstDataRow?: number }`
  - `export function deduplicateHeader(names: string[]): string[]`
  - `export async function listSheetNames(file: File): Promise<string[]>`
  - `export async function readSourceRows(file: File, options: SourceParseOptions, onHeader: (columns: string[]) => void, onRow: (row: string[]) => void): Promise<void>`
  - `export async function readSourceHeader(file: File, options?: SourceParseOptions): Promise<string[]>`
  - `export async function readSourcePreview(file: File, rowCount: number): Promise<string[][]>`（原始前若干行，**不受 headerRow 影响**）
  - `export { parseDelimitedHeaderLine, MAX_XLSX_BYTES }`

- [ ] **Step 1: 写失败的测试**

`frontend/src/admin/schemaEtlConfigBuilder/sourceParser.test.ts`：

```ts
import { describe, expect, it } from 'vitest'
import { readSourceHeader, readSourcePreview, readSourceRows } from './sourceParser'

function csvFile(name: string, text: string): File {
  return new File([text], name, { type: 'text/csv' })
}

describe('readSourceHeader', () => {
  it('缺省读第一行——存量行为不能变', async () => {
    const file = csvFile('a.csv', 'Color,Size\nNatural,S\n')

    expect(await readSourceHeader(file)).toEqual(['Color', 'Size'])
  })

  it('按 headerRow 取表头', async () => {
    const file = csvFile('a.csv', '标题带,\nmd_no,color\nM1AG702,Natural\n')

    expect(await readSourceHeader(file, { headerRow: 2 })).toEqual(['md_no', 'color'])
  })

  it('重名列加后缀——必须跟后端一致，否则第二列在界面上无法寻址', async () => {
    const file = csvFile('a.csv', 'Color,Color\na,b\n')

    expect(await readSourceHeader(file)).toEqual(['Color', 'Color (2)'])
  })

  it('表头行号超出文件长度时返回空', async () => {
    const file = csvFile('a.csv', 'a,b\n1,2\n')

    expect(await readSourceHeader(file, { headerRow: 9 })).toEqual([])
  })
})

describe('readSourceRows', () => {
  it('跳过表头和首数据行之间的说明行', async () => {
    const file = csvFile('a.csv', 'Article Code,Color\n-,(half width 100)\nM1AG702,Natural\n')
    const rows: string[][] = []

    await readSourceRows(
      file,
      { headerRow: 1, firstDataRow: 3 },
      () => {},
      (r) => rows.push(r),
    )

    expect(rows).toEqual([['M1AG702', 'Natural']])
  })

  it('缺省下首数据行紧跟表头', async () => {
    const file = csvFile('a.csv', 'a,b\n1,2\n3,4\n')
    const rows: string[][] = []

    await readSourceRows(
      file,
      {},
      () => {},
      (r) => rows.push(r),
    )

    expect(rows).toEqual([
      ['1', '2'],
      ['3', '4'],
    ])
  })
})

describe('readSourcePreview', () => {
  it('给出原始的前几行，不受 headerRow 影响——探测和预览要看的就是原始形状', async () => {
    const file = csvFile('a.csv', '标题带,\nmd_no,color\nM1AG702,Natural\n')

    expect(await readSourcePreview(file, 2)).toEqual([
      ['标题带', ''],
      ['md_no', 'color'],
    ])
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

```
cd frontend && NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2 src/admin/schemaEtlConfigBuilder/sourceParser.test.ts
```
Expected: FAIL，`Failed to resolve import "./sourceParser"`

- [ ] **Step 3: 写 `sourceParser.ts`**

把 `tableHeader.ts` 的 `readDelimitedHeaderColumns` / `readExcelHeaderColumns` / `parseDelimitedHeaderLine` 和 `columnStats.ts` 的私有 `readTableRows` / `readExcelRows` / `readDelimitedRows` / `cellToString` / `MAX_XLSX_BYTES` / `TEXT_CHUNK_BYTES` 全部搬进来，做三件事：

1. 全部接受 `SourceParseOptions`；
2. 表头一律过 `deduplicateHeader`；
3. `parseDelimitedHeaderLine`、`MAX_XLSX_BYTES`、`TEXT_CHUNK_BYTES` 继续导出（既有测试在引用），`columnStats.ts` 和 `tableHeader.ts` 改成从这里 re-export。

`deduplicateHeader` 是后端 `deduplicate_header` 的逐行对译：

```ts
/**
 * 给重名列加 " (2)"、" (3)" 后缀，让每一列都有自己的键。
 *
 * 这是后端 `app/graphrag/etl_staging.py::deduplicate_header` 的对译，两边
 * 必须逐条一致——不一致时，用户在界面上选的列名跟跑批真正使用的列名会指向
 * 不同的列，而且看不出来。判据在 fixtures/header-dedup-cases.json，前后端
 * 测试各读一遍。
 *
 * 空列名彼此也算重名，同样参与去重。
 */
export function deduplicateHeader(names: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const name of names) {
    let candidate = name
    let suffix = 1
    // 表头里可能本来就有一列叫 "Color (2)"，所以不能算出后缀就直接用。
    while (seen.has(candidate)) {
      suffix += 1
      candidate = `${name} (${suffix})`
    }
    seen.add(candidate)
    result.push(candidate)
  }
  return result
}
```

Excel 侧的工作表选择：

```ts
function selectSheetName(
  workbook: { SheetNames: string[] },
  sheet: string | number | undefined,
): string | null {
  if (sheet === undefined) return workbook.SheetNames[0] ?? null
  if (typeof sheet === 'number') return workbook.SheetNames[sheet] ?? null
  return workbook.SheetNames.includes(sheet) ? sheet : null
}
```

选不中返回 `null`，调用方抛出带可选表名列表的错误——**不要**回落到第一张表，理由跟后端 `_select_xlsx_sheet` 一样：回落会让用户拿到一份完全不相干的数据，而且不报错。

`readSourceRows` 的行跳过语义（CSV 与 Excel 共用）：表头取第 `headerRow` 行（1-based，缺省 1），数据从 `firstDataRow ?? headerRow + 1` 行起。

CSV 侧继续沿用既有的按字节分块 + `parseDelimitedHeaderLine` 实现。**当前实现不支持跨行的引号字段**（后端支持），在 `readSourceRows` 的注释里写明这一限制，不要假装支持——对账机制会在真出现这种文件时把它报出来。

- [ ] **Step 4: 改 `tableHeader.ts`**

整个文件缩成一层薄封装，解析实现不再留在这里：

```ts
import { readSourceHeader } from './sourceParser'
import type { SourceParseOptions } from './sourceParser'

/**
 * 读一张表的列名。解析实现在 sourceParser.ts——这个文件曾经自己实现了一份，
 * columnStats.ts 又实现了另一份，两份都写死"第一个 sheet、第一行表头"，且
 * 规则会悄悄分叉（2026-09-15 的 9c71cf9 让它们分叉过一次）。现在只剩一份。
 */
export async function readTableHeaderColumns(
  file: File,
  options?: SourceParseOptions,
): Promise<string[]> {
  return readSourceHeader(file, options)
}

export { parseDelimitedHeaderLine } from './sourceParser'
```

- [ ] **Step 5: 改 `columnStats.ts`**

删掉私有的 `readTableRows` / `readExcelRows` / `readDelimitedRows` / `cellToString`，改成从 `sourceParser` import；`scanTableFile` 与 `scanPairs` 各加一个 `options: SourceParseOptions = {}` 参数并透传：

```ts
export async function scanTableFile(
  file: File,
  options: SourceParseOptions = {},
): Promise<ColumnStats[]> {
  let acc: StatsAccumulator | null = null
  await readSourceRows(
    file,
    options,
    (columns) => {
      acc = createAccumulator(columns)
    },
    (row) => {
      if (acc !== null) accumulateRow(acc, row)
    },
  )
  if (acc === null) return []
  return finalizeStats(acc)
}
```

`MAX_XLSX_BYTES` 与 `TEXT_CHUNK_BYTES` 改成从 `sourceParser` re-export——既有测试在引用这两个常量。

**保留不动：** `scanTableFile` docstring 里那句「文件不上传——建模阶段数据不出用户的机器」。这是本计划不加后端预览端点的直接原因，删掉它下一个人就会把端点加回来。

- [ ] **Step 6: 跑前端全量**

```
cd frontend && NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2
```
Expected: 全绿，含 Task 4 留下的 `headerDedupCases.test.ts`（基线 757 passed + 本计划新增）

- [ ] **Step 7: 变异测试**

| 变异 | 期望变红 |
|---|---|
| `deduplicateHeader` 直接 `return names` | `headerDedupCases.test.ts` 多条 + `readSourceHeader` 重名列那条 |
| `while (seen.has(candidate))` 改成 `if` | fixture 里"本来就有带后缀的名字"那条 |
| `readSourceRows` 忽略 `firstDataRow` | 「跳过说明行」那条 |
| `selectSheetName` 选不中时回落到 `SheetNames[0]` | 本步骤补一条"指定不存在的 sheet 要报错"的 xlsx 用例 |

- [ ] **Step 8: 提交**

```bash
git add frontend/src/admin/schemaEtlConfigBuilder/sourceParser.ts \
        frontend/src/admin/schemaEtlConfigBuilder/sourceParser.test.ts \
        frontend/src/admin/schemaEtlConfigBuilder/tableHeader.ts \
        frontend/src/admin/guidedOntology/columnStats.ts
git commit -m "refactor(admin): 前端两份解析器收敛成 sourceParser，并跟后端同规则去重"
```

---

### Task 6: 表头行自动探测

**Files:**
- Create: `frontend/src/admin/schemaEtlConfigBuilder/detectHeaderRow.ts`
- Create: `frontend/src/admin/schemaEtlConfigBuilder/detectHeaderRow.test.ts`

**Interfaces:**
- Produces: `export function detectHeaderRow(rows: string[][]): number`（返回 1-based 行号）、`export const DETECT_SCAN_ROWS = 20`

- [ ] **Step 1: 写失败的测试**

```ts
import { describe, expect, it } from 'vitest'
import { detectHeaderRow } from './detectHeaderRow'

describe('detectHeaderRow', () => {
  it('普通的表：第一行就是表头', () => {
    const rows = [
      ['name', 'code'],
      ['foo', 'A1'],
      ['bar', 'A2'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })

  it('MUJI 的形状：合并标题带在第一行，真表头在下面', () => {
    const rows = [
      ['', '', 'Product Information', '', ''],
      ['', '', '', '', ''],
      ['', 'update', 'Continue', 'RKJ Division', 'Dept'],
      ['', '', '新規継続区分', '部門', 'デパ'],
      ['Character Limit', 'Please enter', '-', '-', '-'],
      ['nr', 'update_flag', 'continue_discontinue', 'sel_div', 'sel_depa'],
      ['1', 'Y', '5:Sales End', '1', '11'],
      ['2', 'Y', '5:Sales End', '1', '11'],
      ['3', 'Y', '5:Sales End', '1', '11'],
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('顶上的说明块比真表头还满，但它下面没有数据', () => {
    // 这条用例是"乘上其后几行非空率"那一项存在的唯一理由。只按非空单元格
    // 数打分的话，这里会选中第 1 行——而第 1 行是一段说明文字，不是表头。
    const rows = [
      ['注意', '本表仅供内部使用', '如有疑问请联系', '数据部', '2026'],
      ['', '', '', '', ''],
      ['', '', '', '', ''],
      ['', '', '', '', ''],
      ['', '', '', '', ''],
      ['jan', 'color', 'size', '', ''],
      ['4934761229522', 'Natural', 'S', '', ''],
      ['4934761229539', 'Natural', 'M', '', ''],
      ['4934761229546', 'Natural', 'L', '', ''],
      ['4934761229645', 'Black', 'S', '', ''],
      ['4934761229652', 'Black', 'M', '', ''],
    ]

    expect(detectHeaderRow(rows)).toBe(6)
  })

  it('只有一行时返回第一行', () => {
    expect(detectHeaderRow([['a', 'b']])).toBe(1)
  })

  it('空表返回第一行', () => {
    expect(detectHeaderRow([])).toBe(1)
  })

  it('并列时取最靠上的一行', () => {
    // 靠上的那一行更可能是表头：表头之后才是数据，数据行长得跟表头一样满。
    const rows = [
      ['a', 'b'],
      ['1', '2'],
      ['3', '4'],
    ]

    expect(detectHeaderRow(rows)).toBe(1)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

```
cd frontend && NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2 src/admin/schemaEtlConfigBuilder/detectHeaderRow.test.ts
```
Expected: FAIL，`Failed to resolve import "./detectHeaderRow"`

- [ ] **Step 3: 实现**

```ts
/** 只看前这么多行。表头不可能在第 20 行以后，多扫只是浪费。 */
export const DETECT_SCAN_ROWS = 20

/** 打分时往下看几行。 */
const LOOKAHEAD_ROWS = 5

/**
 * 猜表头在第几行（1-based）。
 *
 * **这只是建议，永远不自动生效。** 界面要显示"猜的是第 N 行"并让用户改——
 * 推断提议、人确认，跟 Foundry 的 schema 推断对话框是同一个姿态。自动生效
 * 的推断一旦猜错，用户看到的是一份莫名其妙的数据，而不是一个可以改的选项。
 *
 * 打分 = 该行非空单元格数 × 其后几行的平均非空率。
 *
 * 光看非空单元格数不够：顶上的说明块（"注意：本表仅供内部使用……"）可能比
 * 真表头还满。乘上"其后几行的非空率"就能把它排掉——说明块下面通常是空行，
 * 真表头下面是密密麻麻的数据。
 */
export function detectHeaderRow(rows: string[][]): number {
  if (rows.length < 2) return 1

  let bestRow = 1
  let bestScore = -1
  const limit = Math.min(rows.length, DETECT_SCAN_ROWS)

  for (let i = 0; i < limit; i++) {
    const width = Math.max(rows[i].length, 1)
    const nonEmpty = rows[i].filter((cell) => cell.trim() !== '').length
    const lookahead = rows.slice(i + 1, i + 1 + LOOKAHEAD_ROWS)
    if (lookahead.length === 0) continue
    const fillRate =
      lookahead.reduce(
        (sum, row) => sum + row.filter((cell) => cell.trim() !== '').length / width,
        0,
      ) / lookahead.length
    const score = nonEmpty * fillRate
    // 严格大于：并列时保留更靠上的那一行。表头之后才是数据，而数据行长得
    // 跟表头一样满，分数会打平——这时候靠上的那个才是表头。
    if (score > bestScore) {
      bestScore = score
      bestRow = i + 1
    }
  }
  return bestRow
}
```

- [ ] **Step 4: 跑测试确认通过**

```
cd frontend && NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2 src/admin/schemaEtlConfigBuilder/detectHeaderRow.test.ts
```

- [ ] **Step 5: 变异测试**

| 变异 | 期望变红 |
|---|---|
| `const score = nonEmpty`（去掉乘法项） | 「顶上的说明块比真表头还满」那条 |
| `if (score >= bestScore)` | 「并列时取最靠上的一行」那条 |
| 删掉 `if (rows.length < 2) return 1` | 「只有一行」「空表」两条 |
| `LOOKAHEAD_ROWS = 1` | MUJI 形状那条 |

若最后一条没红，说明 MUJI 用例的行形状不够真实——**不要**因此放过它，把用例改得更接近真实文件（真实文件里第 4 行日文名、第 5 行说明行都相当满）。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/schemaEtlConfigBuilder/detectHeaderRow.ts \
        frontend/src/admin/schemaEtlConfigBuilder/detectHeaderRow.test.ts
git commit -m "feat(admin): 表头行自动探测，只提议不自动生效"
```

---

### Task 7: 表格导入页的「解析设置」

**Files:**
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/types.ts:22-26`（`AddedFile`）
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.ts:62-88`（`buildConfigYaml`）
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/TableImportFlow.tsx:161`（加文件处）及第二步渲染、提交处
- Test: `frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.test.ts`（不存在则新建）、`TableImportFlow` 既有测试文件

**Interfaces:**
- Consumes: `readSourcePreview` / `readSourceHeader` / `listSheetNames` / `SourceParseOptions`（Task 5）、`detectHeaderRow` / `DETECT_SCAN_ROWS`（Task 6）
- Produces: `AddedFile.parseOptions: SourceParseOptions`；YAML 顶部的 `sources:` 段；提交时的 `client_columns` 表单字段

- [ ] **Step 1: 写失败的测试**

`buildConfigYaml.test.ts`：

```ts
it('把每个文件的解析设置写进 sources 段', () => {
  const file = new File([''], 'CN_001_SKU_MASTER_121.xls')
  const yaml = buildConfigYaml({
    tenantId: 'muji',
    entities: [],
    relations: [],
    files: [
      {
        id: 'f1',
        file,
        columns: [],
        parseOptions: { sheet: 'Master', headerRow: 6, firstDataRow: 7 },
      },
    ],
  })

  expect(yaml).toContain('sources:')
  expect(yaml).toContain('  - file: "CN_001_SKU_MASTER_121.xls"')
  expect(yaml).toContain('    sheet: "Master"')
  expect(yaml).toContain('    header_row: 6')
  expect(yaml).toContain('    first_data_row: 7')
})

it('没有改过解析设置的文件不写进 sources 段', () => {
  // 全是缺省值还写一遍，等于把"第 1 行"固化进配置。将来缺省变了，这些
  // 配置不会跟着变，而用户从没做过这个选择。
  const file = new File([''], 'plain.csv')
  const yaml = buildConfigYaml({
    tenantId: 'muji',
    entities: [],
    relations: [],
    files: [{ id: 'f1', file, columns: [], parseOptions: {} }],
  })

  expect(yaml).not.toContain('sources:')
})

it('sheet 是序号时按数字写，不加引号', () => {
  // 写成 sheet: "1" 的话，后端会把它当成一张名叫 "1" 的工作表去找。
  const file = new File([''], 'a.xls')
  const yaml = buildConfigYaml({
    tenantId: 'muji',
    entities: [],
    relations: [],
    files: [{ id: 'f1', file, columns: [], parseOptions: { sheet: 1 } }],
  })

  expect(yaml).toContain('    sheet: 1')
})
```

追加到 `frontend/src/admin/schemaEtlPage.test.tsx`，沿用该文件既有的
`signIn` / `stubDemoMapping` / `renderAt` / `chooseFile` / `requests` 夹具。
先在文件顶部的辅助函数区加一个造"多层表头"文件的函数：

```tsx
/** 第 1 行是标题带、第 2 行才是真表头的表——MUJI 那张主数据表的形状。 */
function layeredCsv(name = 'layered.csv') {
  return new File(
    [`商品情報,,,
${DEMO_HEADER}
A,1,张三,100000
`],
    name,
    { type: 'text/csv' },
  )
}
```

```tsx
describe('解析设置', () => {
  it('选完文件后显示猜出来的表头行，并说明它是猜的', async () => {
    // 探测结果自动生效而不告诉用户的话，猜错时他看到的是一份莫名其妙的
    // 列名列表，而且不知道有个开关可以改。
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, layeredCsv())

    const settings = within(flow).getByTestId('parse-settings')
    expect(settings.textContent).toMatch(/自动识别/)
    expect(settings.textContent).toMatch(/第 2 行/)
  })

  it('改了表头行之后列名跟着变', async () => {
    // 探测只是建议。改不动的建议等于自动生效。
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, layeredCsv())
    const settings = within(flow).getByTestId('parse-settings')
    const headerRowInput = within(settings).getByLabelText(/表头行/) as HTMLInputElement
    await user.clear(headerRowInput)
    await user.type(headerRowInput, '1')

    await waitFor(() => {
      // 第 1 行是标题带：只有第一格有内容，其余是空列名。
      expect(within(flow).getByTestId('parse-settings').textContent).toMatch(/商品情報/)
    })
  })

  it('首数据行跳过一段时，说清会跳过哪几行', async () => {
    // 填大了会安静地少读数据。不显示的话用户看不出自己丢了几行。
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, layeredCsv())
    const settings = within(flow).getByTestId('parse-settings')
    const firstDataRow = within(settings).getByLabelText(/首数据行/) as HTMLInputElement
    await user.clear(firstDataRow)
    await user.type(firstDataRow, '5')

    await waitFor(() => {
      expect(within(flow).getByTestId('parse-settings').textContent).toMatch(/跳过第 3~4 行/)
    })
  })

  it('提交时带上页面上看到的列名', async () => {
    // 前端本地解析、后端跑批解析，两份规则会悄悄分叉——9c71cf9 已经让它们
    // 分叉过一次。不把页面看到的列名发过去，后端就无从对账。
    signIn('admin')
    stubDemoMapping()
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, layeredCsv())
    await user.click(within(flow).getByTestId('run-import'))

    await waitFor(() => {
      const posted = requests.find(
        (r) => r.url.includes('/schema-etl/runs') && r.init?.method === 'POST',
      )
      expect(posted).toBeTruthy()
      const body = posted!.init!.body as FormData
      const declared = JSON.parse(body.get('client_columns') as string)
      expect(declared['layered.csv']).toEqual(DEMO_HEADER.split(','))
    })
  }, 20000)

  it('存着的映射带解析设置时，重开页面要回填，而不是退回第 1 行', async () => {
    // 摘要里存了"表头在第 2 行"，界面却显示第 1 行的话，用户看到的是一份
    // 他没配过的设置，而且没有任何提示说设置被改了。
    signIn('admin')
    stubEtlMapping({
      config_yaml: 'entities: []',
      source_file_name: 'layered.csv',
      created_at: '2026-09-15T00:00:00',
      summary: {
        ...DEMO_SUMMARY,
        sources: [{ file: 'layered.csv', sheet: null, header_row: 2, first_data_row: null }],
      },
    })
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.etl)

    const flow = await chooseFile(user, layeredCsv())

    const settings = within(flow).getByTestId('parse-settings')
    expect((within(settings).getByLabelText(/表头行/) as HTMLInputElement).value).toBe('2')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

```
cd frontend && NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2 src/admin/schemaEtlConfigBuilder/
```

- [ ] **Step 3: 改 `types.ts`**

```ts
import type { SourceParseOptions } from './sourceParser'

export interface AddedFile {
  id: string
  file: File
  columns: string[]
  /** 这张表怎么读。空对象 = 全用缺省（第一个工作表、第一行表头）。 */
  parseOptions: SourceParseOptions
}
```

- [ ] **Step 4: 改 `buildConfigYaml.ts`**

在 `tenant_id` 之后、`entities` 之前插入：

```ts
  // 只写用户真正改过的那些。全是缺省值也写一遍，等于把"第 1 行"固化进配置；
  // 将来缺省变了，这些配置不会跟着变，而用户从没做过这个选择。
  const configured = params.files.filter((f) => hasNonDefaultParseOptions(f.parseOptions))
  if (configured.length > 0) {
    lines.push('sources:')
    for (const f of configured) {
      lines.push(`  - file: ${yamlString(f.file.name)}`)
      const { sheet, headerRow, firstDataRow } = f.parseOptions
      if (sheet !== undefined) {
        // 序号是数字标量，名字是字符串标量——写成 sheet: "1" 的话后端会把它
        // 当成一张名叫 "1" 的工作表去找，找不到就报错。
        lines.push(`    sheet: ${typeof sheet === 'number' ? sheet : yamlString(sheet)}`)
      }
      if (headerRow !== undefined) lines.push(`    header_row: ${headerRow}`)
      if (firstDataRow !== undefined) lines.push(`    first_data_row: ${firstDataRow}`)
    }
    lines.push('')
  }
```

配套的判定：

```ts
function hasNonDefaultParseOptions(options: SourceParseOptions): boolean {
  return (
    options.sheet !== undefined ||
    (options.headerRow !== undefined && options.headerRow !== 1) ||
    options.firstDataRow !== undefined
  )
}
```

- [ ] **Step 5: 改 `TableImportFlow.tsx`**

先给 `EtlMappingSummary`（`frontend/src/admin/etlMappingApi.ts:4`）补上后端 Task 2 新输出的那一段：

```ts
export interface StoredSourceParseOptions {
  file: string
  sheet: string | number | null
  header_row: number
  first_data_row: number | null
}

export interface EtlMappingSummary {
  // …既有字段不动
  /** staging 层的解析选项。存量摘要没有这一段，按缺省解释。 */
  sources?: StoredSourceParseOptions[]
}
```

新建 `frontend/src/admin/schemaEtlConfigBuilder/storedParseOptions.ts`：

```ts
import type { EtlMappingSummary, StoredSourceParseOptions } from '../etlMappingApi'
import type { SourceParseOptions } from './sourceParser'

/**
 * 把存着的解析选项翻译回编辑器用的形状。
 *
 * 存了不回填的话，用户重开页面看到的是"表头在第 1 行"——一份他没配过的
 * 设置，而且没有任何提示说设置被换掉了。他多半会以为配置丢了，重配一遍。
 *
 * 后端用 snake_case、null 表示"没设"；编辑器用 camelCase、undefined 表示
 * "没设"。两套约定各有理由（YAML 惯例 / TS 惯例），翻译集中在这一处，不
 * 散到组件里。
 */
export function storedParseOptionsFor(
  summary: EtlMappingSummary | null | undefined,
  fileName: string,
): SourceParseOptions | null {
  const stored: StoredSourceParseOptions | undefined = summary?.sources?.find(
    (s) => s.file === fileName,
  )
  if (!stored) return null
  const options: SourceParseOptions = {}
  if (stored.sheet !== null) options.sheet = stored.sheet
  if (stored.header_row !== 1) options.headerRow = stored.header_row
  if (stored.first_data_row !== null) options.firstDataRow = stored.first_data_row
  return options
}
```

配套测试 `storedParseOptions.test.ts`：

```ts
import { describe, expect, it } from 'vitest'
import { storedParseOptionsFor } from './storedParseOptions'

const summary = {
  entities: [],
  relations: [],
  sources: [{ file: 'a.xls', sheet: 'Master', header_row: 6, first_data_row: 7 }],
} as never

describe('storedParseOptionsFor', () => {
  it('按文件名取出存着的选项', () => {
    expect(storedParseOptionsFor(summary, 'a.xls')).toEqual({
      sheet: 'Master',
      headerRow: 6,
      firstDataRow: 7,
    })
  })

  it('这张表没存过就返回 null，让探测接手', () => {
    expect(storedParseOptionsFor(summary, 'b.csv')).toBeNull()
  })

  it('存量摘要没有 sources 段时返回 null，不报错', () => {
    expect(storedParseOptionsFor({ entities: [], relations: [] } as never, 'a.xls')).toBeNull()
  })

  it('null 翻译成"没设"，不翻译成字面 null', () => {
    // 把 sheet: null 原样塞进 parseOptions 的话，YAML 会写出 sheet: null，
    // 后端会拿它去找一张名叫 "null" 的工作表。
    const s = {
      entities: [],
      relations: [],
      sources: [{ file: 'a.csv', sheet: null, header_row: 1, first_data_row: null }],
    } as never

    expect(storedParseOptionsFor(s, 'a.csv')).toEqual({})
  })
})
```

加文件处（`TableImportFlow.tsx:161`）改为：**存着的设置优先，没有才跑探测**。

```tsx
// 存着的设置是用户自己配的，优先于探测——探测只是没配过时的兜底。反过来
// 的话，用户每次重开页面都要再改一遍他上次已经改好的设置。
const stored = storedParseOptionsFor(mapping?.summary ?? null, file.name)
let parseOptions: SourceParseOptions
if (stored !== null) {
  parseOptions = stored
} else {
  const preview = await readSourcePreview(file, DETECT_SCAN_ROWS)
  const headerRow = detectHeaderRow(preview)
  // 探测结果直接落进 parseOptions，但界面上必须标明它是猜的、可以改。
  parseOptions = headerRow === 1 ? {} : { headerRow }
}
added.push({
  id: crypto.randomUUID(),
  file,
  columns: await readSourceHeader(file, parseOptions),
  parseOptions,
})
```

「自动识别：第 N 行」那句**只在走了探测分支时**显示；走存储分支时改显示「沿用上次配置」——两者来源不同，混成一句话会让用户以为系统每次都在猜。

第二步顶部加一个「解析设置」区块，`data-testid="parse-settings"`，包含：

- 工作表下拉（仅 `.xlsx`/`.xls`，选项来自 `listSheetNames(file)`）
- 表头行数字输入，旁边一句 `自动识别：第 N 行`（用户改过之后这句仍显示原探测值，让他知道自己偏离了多少）
- 首数据行数字输入，留空表示紧跟表头
- 当 `firstDataRow > headerRow + 1` 时显示 `将跳过第 X~Y 行`——**这条必须有**，否则填大了会安静地少读数据
- 前 5 行的原始预览表格，表头行那一行高亮

任何一项变化都重算 `columns = await readSourceHeader(file, parseOptions)`，并把受影响的映射交给既有的 `prefillMapping` 重新跑一遍（列名变了，存着的映射可能对不上，这条路径既有代码已经处理）。

提交处在 `FormData` 里追加：

```tsx
// 把页面上看到的列名一并发给后端对账。前端本地解析、后端跑批解析，两份
// 规则会悄悄分叉——9c71cf9 已经让它们分叉过一次。不对账的话，用户拿到的
// 结果跟他在界面上看到的不一样，而且没有任何提示。
formData.append(
  'client_columns',
  JSON.stringify(Object.fromEntries(files.map((f) => [f.file.name, f.columns]))),
)
```

- [ ] **Step 6: 跑前端全量**

```
cd frontend && NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2
```

- [ ] **Step 7: 变异测试**

| 变异 | 期望变红 |
|---|---|
| `hasNonDefaultParseOptions` 永远返回 `true` | 「没有改过解析设置的文件不写进 sources 段」 |
| `sheet` 一律走 `yamlString` | 「sheet 是序号时按数字写」 |
| 提交时不 append `client_columns` | 「提交时带上页面上看到的列名」 |
| 加文件时不跑 `detectHeaderRow` | 「显示猜出来的表头行」 |
| `storedParseOptionsFor` 永远返回 `null` | 「存着的映射带解析设置时…要回填」 |
| `storedParseOptionsFor` 把 `sheet: null` 原样带出 | 「null 翻译成"没设"」 |
| 探测分支优先于存储分支 | 「存着的映射带解析设置时…要回填」 |

- [ ] **Step 8: 用真实文件人工验收**

重启前端，把 `D:\doc\深演项目\muji\CN_001_SKU_MASTER_121.xls` 拖进表格导入：

1. 解析设置里工作表下拉能看到 `Master` / `Invalid_JAN_list` / `List` / `Work` / `Work2`
2. 表头行自动识别为 **6**
3. 列名是 113 个系统代码（`nr` / `update_flag` / … / `country_sel_tm_en_cn`），不是 `Product Information`
4. 字段映射里能把 `jan` 选成身份键
5. 点开始导入不报"列名对不上"

任何一条不成立都是这个计划没做完，不要跳过。

- [ ] **Step 9: 提交**

```bash
git add frontend/src/admin/schemaEtlConfigBuilder/types.ts \
        frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.ts \
        frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.test.ts \
        frontend/src/admin/schemaEtlConfigBuilder/storedParseOptions.ts \n        frontend/src/admin/schemaEtlConfigBuilder/storedParseOptions.test.ts \n        frontend/src/admin/etlMappingApi.ts \n        frontend/src/admin/schemaEtlConfigBuilder/TableImportFlow.tsx \n        frontend/src/admin/schemaEtlPage.test.tsx
git commit -m "feat(admin): 表格导入加解析设置，选工作表和表头行"
```

---

## 收尾检查

- [ ] `python -u -m pytest tests/ -q` 全绿
- [ ] `cd frontend && NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2` 全绿
- [ ] `grep -rn "2026-08-21-schema-etl-multi-format-upload" app/ frontend/src/` 无结果（失效引用清干净）
- [ ] Task 7 Step 8 的五条人工验收全部通过
- [ ] spec 「未决风险」里那条 **CSV 编码前后端不一致**（后端有 UTF-8→GBK 回落、前端写死 utf-8）：对账机制上线后它会第一次暴露出来。**这不是新 bug**。若实测中被报出来，单独开一个任务修，**不要**因为"报警太吵"把对账关掉。
