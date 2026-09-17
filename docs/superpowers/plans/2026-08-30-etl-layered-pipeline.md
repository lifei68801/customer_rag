# ETL 分层管道与写入前主键校验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `schema_etl.py` 的"边读边写"拆成 staging → projection → 写入三层，让 `node_key` 重复在写入之前被发现并整体失败，而不是跑到一半开始逐行跳过。

**Architecture:** 解析逻辑原样搬到 `etl_staging.py`；新建 `etl_projection.py` 负责把源行物化成 `node_key`/展示名/属性。projection 分两遍读文件：第一遍流式只算 `node_key` + 行号做查重（内存 O(行数)），第二遍流式重算并交给写入层。`run_schema_etl` 在所有实体映射的第一遍都跑完、确认无重复键之后，才进入写入。

**Tech Stack:** Python 3.12、aiosqlite、openpyxl（xlsx 流式）、xlrd（xls）、pytest + anyio。

**Spec:** [docs/superpowers/specs/2026-08-30-etl-layered-pipeline-design.md](../specs/2026-08-30-etl-layered-pipeline-design.md)

## Global Constraints

- `compute_node_key` 的实现与 `node_key_parts` 的配置形状不改动，只改变调用位置。
- staging 层的提取必须是纯搬运：编码探测、幽灵行跳过、类型归一的行为一行不改。
- 行级脏数据（缺列、类型转换失败）仍然是跳过 + 记报告；只有主键重复升级为整体失败。
- 关系写入路径的端点存在性守卫保持不变。
- 三层都在进程内传递，不引入持久化的中间数据集。
- 测试基线 **1433 passed**。每个任务结束时全量必须是 `0 failed`。
- **pytest 会在打完 summary 后卡在 teardown**（aiosqlite 工作线程非 daemon）。跑测试一律用：
  `timeout 400 python -m pytest -q > /tmp/o.txt 2>&1; grep -E "passed|failed" /tmp/o.txt | tail -2`
  退出码 124 是预期的，不是失败。需要时设 `PYTHONPATH="D:/project/customer_rag"` 和 `PYTHONIOENCODING=utf-8`。
- 注释和文档字符串用中文，跟现有代码一致。

## 两处对 spec 的修正（写计划时实读代码得出，实施时以本节为准）

**修正一 · spec 的"报告结构要变、管理后台要同步"不成立。**
spec 的"未决风险"里说 `ETLRunReport` 需要容纳"整体失败"这个新终态、`admin_schema_etl_routes.py` 与 `etl_runs_store` 的展示逻辑要同步。实读代码后这条不成立：

- `admin_schema_etl_routes.py:174` 的 `_run_schema_etl_job` 已经是 `except Exception as exc:` → `mark_etl_run_failed(conn, run_id=..., error=str(exc))`，任何从 `run_schema_etl` 抛出的异常都会落到 `status='failed'` 并保留完整消息。
- 前端 `frontend/src/admin/SchemaEtlPage.tsx:509` 已经渲染 `失败：{selectedRun.error}`。

因此 `DuplicateNodeKeyError` 直接抛出即可，**`ETLRunReport`、`etl_runs_store`、路由、前端都不需要改动**。本计划不含相关任务。实施者不要"顺手"去改它们。

**修正二 · 解析测试是"新增"而不是"改为"。**
spec 的测试策略说把现有覆盖解析行为的用例"改为直接测 `etl_staging`"。实读后发现这些用例（`test_run_schema_etl_reads_gbk_encoded_csv`、`..._reads_tsv_source_file`、`..._reads_xlsx_source_file`、`..._reads_xls_source_file`、`..._xlsx_empty_sheet_writes_nothing`、`..._xlsx_phantom_trailing_row_is_skipped_not_counted`、`..._reads_utf8_bom_encoded_csv`）全都是走 `run_schema_etl` 的端到端用例。把它们改写成直接测 staging，恰恰会丢掉"搬运没有改变端到端行为"的证据——而那正是纯搬运唯一需要的保险。

**所以：这些端到端用例一条不动、一条不改，另外新增 `tests/graphrag/test_etl_staging.py` 直接覆盖 staging 的接口。** 这比 spec 的写法严格更强。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `app/graphrag/etl_staging.py`（新建） | 第一层。把上传文件解析成统一的 `Iterator[dict[str, str]]`，做类型归一。纯搬运，无新逻辑。 |
| `app/graphrag/etl_projection.py`（新建） | 第二层。两遍接口：`scan_entity_node_keys`（查重）与 `project_entity_rows` / `project_relation_rows`（流式产出带键的行）。`DuplicateNodeKeyError` 与它的消息格式化也在这里。 |
| `app/graphrag/schema_etl.py`（改） | 第三层 + 编排。删掉搬走的解析函数，`_write_entity_mapping` / `_write_relation_mapping` 改为消费 projection，`run_schema_etl` 增加预检阶段。 |
| `tests/graphrag/test_etl_staging.py`（新建） | staging 的直接单测。 |
| `tests/graphrag/test_etl_projection.py`（新建） | projection 两遍的直接单测。 |
| `tests/graphrag/test_schema_etl.py`（改） | 只**新增**整体失败、零写入、稳定码幂等的用例；既有用例一条不改。 |

---

## Task 1: 提取 staging 层（纯搬运）

**Files:**
- Create: `app/graphrag/etl_staging.py`
- Modify: `app/graphrag/schema_etl.py`（删除第 84-206 行的六个解析函数，改为从 `etl_staging` 导入）
- Test: `tests/graphrag/test_etl_staging.py`（新建）

**Interfaces:**
- Consumes: `app.graphrag.schema_etl_row_processing.convert_excel_cell_to_string`、`RowProcessingError`（已存在，不改）
- Produces: `read_table_rows(path: Path) -> Iterator[dict[str, str]]` —— 后续任务唯一需要的入口。其余五个函数保持模块私有（前缀 `_`）。

**背景（实施者需要知道的）：** 今天 `schema_etl.py` 第 84-206 行有六个函数负责解析：`_detect_text_encoding`、`_read_delimited_rows`、`_read_xlsx_rows`、`_xlrd_cell_to_python_value`、`_read_xls_rows`、`_read_table_rows`。它们只被 `_write_entity_mapping`（第 237 行）和 `_write_relation_mapping`（第 330 行）调用。本任务把它们整体搬到新模块，**函数体一个字符都不改**，只有 `_read_table_rows` 去掉下划线前缀变成公开的 `read_table_rows`。

- [ ] **Step 1: 新建 `app/graphrag/etl_staging.py`，把六个函数原样搬过来**

新文件的头部：

```python
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

import xlrd
from openpyxl import load_workbook

from app.graphrag.schema_etl_row_processing import RowProcessingError, convert_excel_cell_to_string
```

然后把 `schema_etl.py` 第 84-206 行的六个函数**逐字复制**过来，顺序保持不变（`_detect_text_encoding` → `_read_delimited_rows` → `_read_xlsx_rows` → `_xlrd_cell_to_python_value` → `_read_xls_rows` → `_read_table_rows`），包括每一条文档字符串和行内注释。唯一的改动是最后一个函数改名：

```python
def read_table_rows(path: Path) -> Iterator[dict[str, str]]:
```

在这个函数的文档字符串末尾追加一段，说明它现在的层次身份：

```python
    """按扩展名分流到对应的行读取器，统一产出 dict[str, str]——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md。

    这是 ETL 三层管道的第一层（staging）的唯一入口：解析 + 类型归一，
    不认识 node_key、不认识本体，只把各种格式的表统一成行序列。见
    docs/superpowers/specs/2026-08-30-etl-layered-pipeline-design.md。
    """
```

在模块顶部加一行模块级文档字符串：

```python
"""ETL 三层管道的第一层：staging——解析与类型归一。

xlsx/csv/tsv/xls → 统一的 dict[str, str] 行序列。这一层不知道本体、
不知道 node_key，只负责"把文件变成行"。2026-08-30 从 schema_etl.py
原样提取，行为一行未改。
"""
```

- [ ] **Step 2: 从 `schema_etl.py` 删除搬走的函数，改为导入**

删除 `schema_etl.py` 第 84-206 行（`_detect_text_encoding` 到 `_read_table_rows` 这六个函数的完整定义）。

在 import 区加入：

```python
from app.graphrag.etl_staging import read_table_rows
```

把两个调用点改名：

- 第 237 行 `enumerate(_read_table_rows(data_dir / mapping.source_file), start=2)` → `enumerate(read_table_rows(data_dir / mapping.source_file), start=2)`
- 第 330 行同样的替换

清理 `schema_etl.py` 里因此不再使用的 import：`csv`、`xlrd`、`from openpyxl import load_workbook`、`convert_excel_cell_to_string`、`Iterator`。**逐个确认**——用 `grep -n "csv\.\|xlrd\.\|load_workbook\|convert_excel_cell_to_string\|Iterator" app/graphrag/schema_etl.py` 检查删干净了没有，也别误删还在用的。

- [ ] **Step 3: 跑全量，确认纯搬运没有改变任何行为**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `1433 passed`，0 failed。**这一步是纯搬运的验收标准**：既有的 7 条端到端解析用例（GBK、UTF-8 BOM、tsv、xlsx、xls、空 sheet、幽灵行）全部仍然通过，就证明搬运没改语义。如果有任何一条挂了，是搬运出错，不要改测试去迁就。

- [ ] **Step 4: 新建 `tests/graphrag/test_etl_staging.py`，直接覆盖 staging 接口**

```python
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from app.graphrag.etl_staging import read_table_rows
from app.graphrag.schema_etl_row_processing import RowProcessingError


def test_read_table_rows_reads_csv_with_utf8_bom(tmp_path: Path):
    """Excel 的 CSV UTF-8 导出会在文件开头写 BOM。utf-8-sig 会剥掉它；
    如果退化成纯 utf-8，BOM 会粘在第一个列名前面，整份文件被误判成缺列。"""
    path = tmp_path / "with_bom.csv"
    path.write_bytes("名称,编号\n抹茶,A1\n".encode("utf-8-sig"))

    rows = list(read_table_rows(path))

    assert rows == [{"名称": "抹茶", "编号": "A1"}]


def test_read_table_rows_falls_back_to_gbk(tmp_path: Path):
    """国内 Excel 导出的 CSV 常见默认编码是 GBK，UTF-8 严格解码会失败。"""
    path = tmp_path / "gbk.csv"
    path.write_bytes("名称,编号\n抹茶,A1\n".encode("gbk"))

    rows = list(read_table_rows(path))

    assert rows == [{"名称": "抹茶", "编号": "A1"}]


def test_read_table_rows_reads_tsv_with_tab_delimiter(tmp_path: Path):
    path = tmp_path / "data.tsv"
    path.write_text("name\tcode\nfoo\tA1\n", encoding="utf-8")

    rows = list(read_table_rows(path))

    assert rows == [{"name": "foo", "code": "A1"}]


def test_read_table_rows_skips_xlsx_phantom_all_empty_rows(tmp_path: Path):
    """openpyxl 的 read_only 迭代会按已用范围补齐行数，被清空的行会
    产出全空的幽灵行。这些行必须安静跳过，不能当成缺列的脏数据。"""
    path = tmp_path / "phantom.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "code"])
    sheet.append(["foo", "A1"])
    sheet.append(["bar", "A2"])
    sheet["A3"] = None
    sheet["B3"] = None
    workbook.save(path)

    rows = list(read_table_rows(path))

    assert rows == [{"name": "foo", "code": "A1"}]


def test_read_table_rows_on_empty_xlsx_sheet_yields_nothing(tmp_path: Path):
    path = tmp_path / "empty.xlsx"
    workbook = Workbook()
    workbook.active.append(["name", "code"])
    workbook.save(path)

    assert list(read_table_rows(path)) == []


def test_read_table_rows_rejects_unsupported_suffix(tmp_path: Path):
    path = tmp_path / "data.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(RowProcessingError, match="不支持的数据文件类型"):
        list(read_table_rows(path))


def test_read_table_rows_is_lazy(tmp_path: Path):
    """staging 必须保持流式：真实规模是 MUJI 一张 SKU 表 18 万+ 行。
    拿到迭代器时文件还没被遍历完，只有真正迭代才逐行产出。"""
    path = tmp_path / "data.csv"
    path.write_text("name\nfoo\nbar\n", encoding="utf-8")

    iterator = read_table_rows(path)
    first = next(iterator)

    assert first == {"name": "foo"}
```

- [ ] **Step 5: 跑新测试文件**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest tests/graphrag/test_etl_staging.py -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `7 passed`。如果 `test_read_table_rows_is_lazy` 失败，说明 `read_table_rows` 不再是生成器——检查搬运时有没有把 `yield from` 改成 `return`。

- [ ] **Step 6: 跑全量并提交**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `1440 passed`（1433 + 7），0 failed。

```bash
git add app/graphrag/etl_staging.py app/graphrag/schema_etl.py tests/graphrag/test_etl_staging.py
git commit -m "refactor(etl): lift table parsing into a staging layer"
```

---

## Task 2: projection 层的实体路径（两遍接口）

**Files:**
- Create: `app/graphrag/etl_projection.py`
- Test: `tests/graphrag/test_etl_projection.py`（新建）

**Interfaces:**
- Consumes: `read_table_rows(path) -> Iterator[dict[str, str]]`（Task 1）；`compute_node_key(conn, *, tenant_id, term_type, node_key_parts, row, allow_allocation=True) -> str`、`convert_field_value(*, extra_field_specs, field_name, raw_value)`、`RowProcessingError`（均已存在，不改）；`EntityMapping`（字段：`term_type`、`source_file`、`standard_name_parts: list[str]`、`node_key_parts`、`field_mappings: dict[str, str]`）；`ExtraFieldSpec`。
- Produces:
  - `ProjectedRow(row_number: int, node_key: str, standard_name: str, extra_properties: dict[str, object])`
  - `RowFailure(row_number: int, reason: str)`
  - `KeyScanResult(duplicate_keys: dict[str, list[int]], scanned_rows: int)`
  - `async def scan_entity_node_keys(conn, *, tenant_id, mapping, data_dir) -> KeyScanResult`
  - `async def project_entity_rows(conn, *, tenant_id, mapping, extra_field_specs, data_dir) -> AsyncIterator[ProjectedRow | RowFailure]`
  - `class DuplicateNodeKeyError(Exception)`
  - `def format_duplicate_key_error(duplicates_by_term_type: dict[str, dict[str, list[int]]]) -> str`

**为什么是两遍（这是本计划最重要的设计决定，实施者不要改）：**
spec 的伪代码写的是 `ProjectionResult.rows: list[ProjectedRow]` —— 一遍读完、全量驻留。**本计划不采用这个形状。** 真实规模是 18 万+ 行，`ProjectedRow` 带着 `extra_properties`，全量驻留的内存上界会随行宽增长。改成两遍：

- 第一遍 `scan_entity_node_keys`：流式，只保留 `node_key` 字符串和行号 → 内存 O(行数)，与行宽无关。
- 第二遍 `project_entity_rows`：流式产出，写入层边消费边写，不攒 list。

代价是 xlsx 解析跑两遍。这个取舍已经定了，**不要"优化"成一遍**。

**`allocated_code` 的副作用（必须理解，否则测试会写错）：**
`compute_node_key` 在 `allow_allocation=True` 时会给首次出现的原始值分配稳定码并写进 `etl_stable_code_registry` 表。实体路径两遍都用 `allow_allocation=True`，所以**第一遍就已经产生了持久化副作用**。这是可接受的：稳定码是幂等分配的（同一 scope + 原始值永远得到同一个码），第二遍会命中第一遍的分配，不会产生新码，也不会漂移。Task 5 会用测试钉住这一点。

- [ ] **Step 1: 写失败的测试 `tests/graphrag/test_etl_projection.py`**

```python
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.graphrag.etl_projection import (
    ProjectedRow,
    RowFailure,
    project_entity_rows,
    scan_entity_node_keys,
)
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import ColumnNodeKeyPart, EntityMapping

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_stable_code_registry_schema(conn)
    return conn


def _write_csv(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mapping() -> EntityMapping:
    return EntityMapping(
        term_type="客户",
        source_file="customers.csv",
        standard_name_parts=["name", "zip"],
        node_key_parts=[ColumnNodeKeyPart(column="name"), ColumnNodeKeyPart(column="zip")],
        field_mappings={"city": "city"},
    )


async def test_scan_reports_no_duplicates_when_keys_are_unique(tmp_path: Path):
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        "张三,200,上海",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(), data_dir=tmp_path
    )

    assert result.duplicate_keys == {}
    assert result.scanned_rows == 2


async def test_scan_collects_duplicate_keys_with_source_row_numbers(tmp_path: Path):
    """行号从 2 起算——第 1 行是表头。同一个 node_key 出现在哪几行，是
    DuplicateNodeKeyError 的消息里唯一能让人定位问题的东西。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        "李四,300,广州",
        "张三,100,深圳",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(), data_dir=tmp_path
    )

    assert result.duplicate_keys == {"客户:张三:100": [2, 4]}
    assert result.scanned_rows == 3


async def test_scan_ignores_row_level_failures(tmp_path: Path):
    """缺列的脏行在第一遍里既不算重复、也不该让扫描崩掉——行级问题由
    第二遍统一记录成 RowFailure，第一遍只关心键的重复。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
        ",200,上海",
    ])
    conn = await _conn()

    result = await scan_entity_node_keys(
        conn, tenant_id="t1", mapping=_mapping(), data_dir=tmp_path
    )

    assert result.duplicate_keys == {}
    assert result.scanned_rows == 2


async def test_project_materializes_key_display_name_and_properties(tmp_path: Path):
    """展示名用 " / " 连接，不用冒号——冒号是 node_key 的分隔符，展示名里
    再用一次会让两者在日志和界面上难以区分。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,100,北京",
    ])
    conn = await _conn()

    rows = [
        r async for r in project_entity_rows(
            conn, tenant_id="t1", mapping=_mapping(),
            extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
            data_dir=tmp_path,
        )
    ]

    assert rows == [
        ProjectedRow(
            row_number=2,
            node_key="客户:张三:100",
            standard_name="张三 / 100",
            extra_properties={"city": "北京"},
        )
    ]


async def test_project_yields_row_failure_for_dirty_rows_and_keeps_going(tmp_path: Path):
    """行级脏数据不中断整批：产出一个 RowFailure，继续处理后面的行。"""
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        ",200,上海",
        "张三,100,北京",
    ])
    conn = await _conn()

    rows = [
        r async for r in project_entity_rows(
            conn, tenant_id="t1", mapping=_mapping(),
            extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
            data_dir=tmp_path,
        )
    ]

    assert len(rows) == 2
    assert isinstance(rows[0], RowFailure)
    assert rows[0].row_number == 2
    assert isinstance(rows[1], ProjectedRow)
    assert rows[1].node_key == "客户:张三:100"


async def test_project_reports_missing_standard_name_column_as_row_failure(tmp_path: Path):
    _write_csv(tmp_path / "customers.csv", [
        "name,zip,city",
        "张三,,北京",
    ])
    conn = await _conn()

    rows = [
        r async for r in project_entity_rows(
            conn, tenant_id="t1", mapping=_mapping(),
            extra_field_specs={"city": ExtraFieldSpec(name="city", value_type="string")},
            data_dir=tmp_path,
        )
    ]

    assert len(rows) == 1
    assert isinstance(rows[0], RowFailure)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest tests/graphrag/test_etl_projection.py -q > /tmp/o.txt 2>&1
grep -E "passed|failed|Error" /tmp/o.txt | tail -3
```

Expected: 收集阶段就报 `ModuleNotFoundError: No module named 'app.graphrag.etl_projection'`。

- [ ] **Step 3: 实现 `app/graphrag/etl_projection.py`**

```python
"""ETL 三层管道的第二层：projection——把 staging 产出的源行物化成
node_key、展示名和属性值。

这一层的存在理由是"算完不立刻写"：键先成为可以检查的数据，主键重复
才可能在写入之前被发现。见
docs/superpowers/specs/2026-08-30-etl-layered-pipeline-design.md。

它也是 Foundry「一个数据集只背书一个对象类型」那条规则在本项目的落点：
一份宽事实表在这一层按 EntityMapping 被切成每个 term_type 一份，物理上
仍是一个上传文件，逻辑上已经是 1:1。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from app.graphrag.etl_staging import read_table_rows
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import EntityMapping
from app.graphrag.schema_etl_row_processing import (
    RowProcessingError,
    compute_node_key,
    convert_field_value,
)

# DuplicateNodeKeyError 的消息里最多列出多少条冲突样例——18 万行的表可能
# 有上万处冲突，全列出来会把日志和界面刷爆。
_MAX_DUPLICATE_SAMPLES = 20


@dataclass(frozen=True)
class ProjectedRow:
    """一行源数据物化之后的结果：可以直接交给写入层，不需要再看源文件。"""

    row_number: int
    node_key: str
    standard_name: str
    extra_properties: dict[str, object]


@dataclass(frozen=True)
class RowFailure:
    """行级脏数据（缺列、类型转换失败）。语义不变：跳过 + 记报告，不中断整批。"""

    row_number: int
    reason: str


@dataclass(frozen=True)
class KeyScanResult:
    """第一遍扫描的产物。只保留键和行号——不保留行本身，内存上界因此
    只跟行数有关，跟行有多宽无关。"""

    duplicate_keys: dict[str, list[int]]
    scanned_rows: int


class DuplicateNodeKeyError(Exception):
    """一个或多个实体类型算出了重复的 node_key，整次运行失败、零写入。

    这不是"某几行数据脏"，是配置层面的错误——node_key_parts 声明的列组合
    不足以唯一标识每一行，跳过多少行都不会让配置变对。部分写入会留下一个
    "看起来成功了、实际缺了一部分"的图谱，比失败更难发现。
    """


async def scan_entity_node_keys(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    mapping: EntityMapping,
    data_dir: Path,
) -> KeyScanResult:
    """第一遍：流式读一遍源文件，只算 node_key，收集重复。

    行级失败（缺列等）在这一遍被静默忽略——它们不是键冲突，而且第二遍
    会把它们统一记录成 RowFailure，在这里记一次会重复计数。

    注意 compute_node_key 在这里仍然 allow_allocation=True，也就是说这一遍
    已经会给首次出现的原始值分配稳定码、写进 etl_stable_code_registry。
    "预检失败则零写入"这个保证对 terms 和 Neo4j 成立，对稳定码注册表不
    成立——稳定码是幂等分配的（同一 scope + 原始值永远得到同一个码），
    重跑会命中已有分配，不会漂移。
    """
    seen: dict[str, list[int]] = {}
    scanned = 0
    for row_number, row in enumerate(read_table_rows(data_dir / mapping.source_file), start=2):
        scanned += 1
        try:
            node_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.term_type,
                node_key_parts=mapping.node_key_parts, row=row,
            )
        except RowProcessingError:
            continue
        seen.setdefault(node_key, []).append(row_number)
    return KeyScanResult(
        duplicate_keys={k: v for k, v in seen.items() if len(v) > 1},
        scanned_rows=scanned,
    )


async def project_entity_rows(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    mapping: EntityMapping,
    extra_field_specs: dict[str, ExtraFieldSpec],
    data_dir: Path,
) -> AsyncIterator[ProjectedRow | RowFailure]:
    """第二遍：流式重读源文件，产出物化后的行。

    刻意不攒成 list 返回：写入层边消费边写，行数据不全量驻留内存。第一遍
    已经保证了没有重复键，这一遍只管把每一行算出来。
    """
    for row_number, row in enumerate(read_table_rows(data_dir / mapping.source_file), start=2):
        try:
            node_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.term_type,
                node_key_parts=mapping.node_key_parts, row=row,
            )
            missing = [c for c in mapping.standard_name_parts if not row.get(c)]
            if missing:
                raise RowProcessingError(
                    f"standard_name 需要的列 {missing!r} 不存在或为空"
                )
            # 用 " / " 连接，不用冒号——冒号是 node_key 的分隔符，展示名里
            # 再用一次会让两者在日志和界面上难以区分。
            standard_name = " / ".join(row[c] for c in mapping.standard_name_parts)
            extra_properties = {
                field_name: convert_field_value(
                    extra_field_specs=extra_field_specs, field_name=field_name,
                    raw_value=row[source_column],
                )
                for field_name, source_column in mapping.field_mappings.items()
                if source_column in row and row[source_column]
            }
        except RowProcessingError as exc:
            yield RowFailure(row_number=row_number, reason=str(exc))
            continue
        yield ProjectedRow(
            row_number=row_number, node_key=node_key,
            standard_name=standard_name, extra_properties=extra_properties,
        )


def format_duplicate_key_error(
    duplicates_by_term_type: dict[str, dict[str, list[int]]]
) -> str:
    """把汇总的重复键渲染成一条能直接定位问题的消息。

    最多列出 _MAX_DUPLICATE_SAMPLES 条样例并注明总数——18 万行的表可能有
    上万处冲突，全列出来会把管理后台的失败详情刷爆。
    """
    lines: list[str] = []
    for term_type, duplicates in duplicates_by_term_type.items():
        total = len(duplicates)
        lines.append(
            f"实体类型 {term_type!r} 的 node_key 有 {total} 处重复，本次未写入任何数据。"
        )
        lines.append(
            "配置里 node_key_parts 声明的列组合不足以唯一标识每一行，请检查："
        )
        for node_key, row_numbers in list(duplicates.items())[:_MAX_DUPLICATE_SAMPLES]:
            rows_text = ", ".join(str(n) for n in row_numbers)
            lines.append(f"  {node_key}  ← 源文件第 {rows_text} 行")
        if total > _MAX_DUPLICATE_SAMPLES:
            lines.append(f"  ...（另有 {total - _MAX_DUPLICATE_SAMPLES} 处，完整清单见运行报告）")
    return "\n".join(lines)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest tests/graphrag/test_etl_projection.py -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `6 passed`。

- [ ] **Step 5: 跑全量并提交**

Expected: `1446 passed`（1440 + 6），0 failed。

```bash
git add app/graphrag/etl_projection.py tests/graphrag/test_etl_projection.py
git commit -m "feat(etl): materialize node keys in a projection layer"
```

---

## Task 3: 实体写入层改为消费 projection

**Files:**
- Modify: `app/graphrag/schema_etl.py`（`_write_entity_mapping`，Task 1 之后位于第 100 行附近）

**Interfaces:**
- Consumes: `project_entity_rows(conn, *, tenant_id, mapping, extra_field_specs, data_dir) -> AsyncIterator[ProjectedRow | RowFailure]`、`RowFailure`（Task 2）
- Produces: `_write_entity_mapping` 的签名与外部行为完全不变——它仍然接收 `conn`/`graph_client`/`tenant_id`/`mapping`/`data_dir`/`report`，仍然把结果写进 `report`。

**这个任务的验收标准是"什么都没变"：** 全部既有测试原样通过。`_write_entity_mapping` 从"自己读文件 + 自己算键 + 写"变成"消费 projection + 写"，报告里的计数、跳过原因、写入顺序一律不变。

- [ ] **Step 1: 改写 `_write_entity_mapping` 的循环体**

保留函数开头的 term_type 校验不动：

```python
    term_types = await list_term_types(conn, tenant_id, status="confirmed")
    types_by_value = {t.value: t for t in term_types}
    if mapping.term_type not in types_by_value:
        raise RowProcessingError(f"term_type {mapping.term_type!r} 不在已确认 schema 里")
    extra_field_specs = {f.name: f for f in types_by_value[mapping.term_type].extra_fields}
```

把它后面的整个 `for row_number, row in enumerate(read_table_rows(...), start=2):` 循环替换成：

```python
    # 这一层不再自己读文件、不再自己算键——那两件事已经在 projection 层
    # 做完了（见 etl_projection.py）。这里只负责"把算好的行写进两个存储"。
    async for projected in project_entity_rows(
        conn, tenant_id=tenant_id, mapping=mapping,
        extra_field_specs=extra_field_specs, data_dir=data_dir,
    ):
        if isinstance(projected, RowFailure):
            report.entities_skipped += 1
            _record_skipped_row(
                report, label=mapping.term_type, source_file=mapping.source_file,
                row_number=projected.row_number, reason=projected.reason,
            )
            continue
        try:
            await upsert_term_with_node_key(
                conn, tenant_id=tenant_id, node_key=projected.node_key,
                standard_name=projected.standard_name, aliases=[],
                term_type=mapping.term_type, extra_properties=projected.extra_properties,
            )
            term = Term(
                tenant_id=tenant_id, node_key=projected.node_key,
                standard_name=projected.standard_name, aliases=[],
                term_type=mapping.term_type, extra_properties=projected.extra_properties,
            )
            await graph_client.sync_term(term)
            report.entities_written += 1
            _record_written(report, label=mapping.term_type)
        except (TermNameConflictError, UnknownCategoryError) as exc:
            # RowProcessingError 不在这里捕获了——它只可能来自 projection 层，
            # 而 projection 已经把它转成 RowFailure。这里剩下的是写入本身
            # 才会抛的两种：别名/名字冲突，和属性值引用了未声明的分类。
            report.entities_skipped += 1
            _record_skipped_row(
                report, label=mapping.term_type, source_file=mapping.source_file,
                row_number=projected.row_number, reason=str(exc),
            )
```

在 import 区加入：

```python
from app.graphrag.etl_projection import RowFailure, project_entity_rows
```

- [ ] **Step 2: 跑全量，确认行为一字未变**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest -q > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `1446 passed`，0 failed。**一条既有用例都不该改。** 如果有用例挂了，是改写引入了行为差异，回去看差在哪里，不要改测试。

特别留意 `tests/graphrag/test_schema_etl.py` 里所有断言 `report.entities_skipped` 和 `report.skipped_rows` 的用例——它们正是覆盖实体跳过路径的。

- [ ] **Step 3: 提交**

```bash
git add app/graphrag/schema_etl.py
git commit -m "refactor(etl): let entity writes consume projected rows"
```

---

## Task 4: 关系路径的 projection

**Files:**
- Modify: `app/graphrag/etl_projection.py`（新增关系侧的两个符号）
- Modify: `app/graphrag/schema_etl.py`（`_write_relation_mapping`）
- Test: `tests/graphrag/test_etl_projection.py`（追加用例）

**Interfaces:**
- Consumes: `read_table_rows`、`compute_node_key(..., allow_allocation=False)`
- Produces:
  - `ProjectedRelationRow(row_number: int, subject_node_key: str, object_node_key: str)`
  - `async def project_relation_rows(conn, *, tenant_id, mapping, subject_entity, object_entity, data_dir) -> AsyncIterator[ProjectedRelationRow | RowFailure]`

**关系路径没有查重预检。** 边是 MERGE 的，同一条边从多行产生是合法的（"这两个值在多行里同时出现过"），不是配置错误。spec 也没有要求关系侧查重。**不要顺手加。**

**端点存在性守卫留在写入层，不下沉到 projection。** 那道守卫需要 `list_node_keys_by_term_type` 的查询结果，而且它的语义是"写入时刻这个端点在不在术语表里"——属于写入层的判断。projection 只负责算出两个键。

- [ ] **Step 1: 在 `tests/graphrag/test_etl_projection.py` 追加失败的测试**

先把文件顶部的 import 补上 `ProjectedRelationRow`、`project_relation_rows`、`RelationMapping`、`AllocatedCodeNodeKeyPart`，然后在文件末尾追加：

```python
async def test_project_relation_rows_computes_both_endpoint_keys(tmp_path: Path):
    _write_csv(tmp_path / "orders.csv", [
        "order_id,name,zip",
        "O1,张三,100",
    ])
    conn = await _conn()
    subject = EntityMapping(
        term_type="订单", source_file="orders.csv",
        standard_name_parts=["order_id"],
        node_key_parts=[ColumnNodeKeyPart(column="order_id")],
        field_mappings={},
    )
    obj = _mapping()
    relation = RelationMapping(
        relation_type="ORDER_BY", source_file="orders.csv",
        subject_term_type="订单", object_term_type="客户",
    )

    rows = [
        r async for r in project_relation_rows(
            conn, tenant_id="t1", mapping=relation,
            subject_entity=subject, object_entity=obj, data_dir=tmp_path,
        )
    ]

    assert rows == [
        ProjectedRelationRow(row_number=2, subject_node_key="订单:O1", object_node_key="客户:张三:100")
    ]


async def test_project_relation_rows_never_allocates_new_stable_codes(tmp_path: Path):
    """关系路径必须 allow_allocation=False：这一行引用的实体值如果从没真正
    写入过，不该在关系这一步凭空产生一个新的稳定码、MERGE 出没有对应
    Term 记录的幽灵节点。未命中已有分配 → RowFailure，不是新分配。"""
    _write_csv(tmp_path / "orders.csv", [
        "order_id,name,zip,color",
        "O1,张三,100,红",
    ])
    conn = await _conn()
    subject = EntityMapping(
        term_type="订单", source_file="orders.csv",
        standard_name_parts=["order_id"],
        node_key_parts=[ColumnNodeKeyPart(column="order_id")],
        field_mappings={},
    )
    obj = EntityMapping(
        term_type="颜色", source_file="orders.csv",
        standard_name_parts=["color"],
        node_key_parts=[AllocatedCodeNodeKeyPart(scope_columns=[], raw_value_column="color")],
        field_mappings={},
    )
    relation = RelationMapping(
        relation_type="HAS_COLOR", source_file="orders.csv",
        subject_term_type="订单", object_term_type="颜色",
    )

    rows = [
        r async for r in project_relation_rows(
            conn, tenant_id="t1", mapping=relation,
            subject_entity=subject, object_entity=obj, data_dir=tmp_path,
        )
    ]

    assert len(rows) == 1
    assert isinstance(rows[0], RowFailure)
    assert "稳定码尚未分配" in rows[0].reason
```

**实施者注意：** `AllocatedCodeNodeKeyPart` 的确切字段名请打开 `app/graphrag/schema_etl_config.py` 核对后再写（本计划给的是按 `compute_node_key` 的用法推断的 `scope_columns` / `raw_value_column`）。如果字段名不同，按实际的改，并在报告里说明。

- [ ] **Step 2: 跑测试确认失败**

Expected: `ImportError: cannot import name 'project_relation_rows'`。

- [ ] **Step 3: 在 `etl_projection.py` 里实现关系侧**

在 `ProjectedRow` 后面加数据类：

```python
@dataclass(frozen=True)
class ProjectedRelationRow:
    """一行源数据算出的两个端点键。关系边的真实含义是"这两个值在某一行里
    同时出现过"——projection 只负责把这两个键算出来，端点在不在术语表里
    是写入层的判断。"""

    row_number: int
    subject_node_key: str
    object_node_key: str
```

在文件末尾（`format_duplicate_key_error` 之前）加：

```python
async def project_relation_rows(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    mapping: RelationMapping,
    subject_entity: EntityMapping,
    object_entity: EntityMapping,
    data_dir: Path,
) -> AsyncIterator[ProjectedRelationRow | RowFailure]:
    """关系侧的 projection：流式算出每一行的两个端点键。

    两端都用 allow_allocation=False——关系路径不该分配新的稳定码，见
    compute_node_key 的说明。未命中已有分配时 compute_node_key 抛
    RowProcessingError，在这里转成 RowFailure。

    这一层没有查重：边是 MERGE 的，同一条边从多行产生是合法的，不像实体
    主键重复那样意味着配置错了。
    """
    for row_number, row in enumerate(read_table_rows(data_dir / mapping.source_file), start=2):
        try:
            subject_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.subject_term_type,
                node_key_parts=subject_entity.node_key_parts, row=row, allow_allocation=False,
            )
            object_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.object_term_type,
                node_key_parts=object_entity.node_key_parts, row=row, allow_allocation=False,
            )
        except RowProcessingError as exc:
            yield RowFailure(row_number=row_number, reason=str(exc))
            continue
        yield ProjectedRelationRow(
            row_number=row_number, subject_node_key=subject_key, object_node_key=object_key,
        )
```

import 区补上 `RelationMapping`：

```python
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping
```

- [ ] **Step 4: 改写 `_write_relation_mapping` 的循环**

`_write_relation_mapping` 开头的四段校验（relation_type 在已确认 schema 里、组合在允许列表里、两端实体类型已声明、预取两端的 node_key 集合）**全部保持不动**。只替换最后那个 `for row_number, row in enumerate(read_table_rows(...), start=2):` 循环：

```python
    async for projected in project_relation_rows(
        conn, tenant_id=tenant_id, mapping=mapping,
        subject_entity=subject_entity, object_entity=object_entity, data_dir=data_dir,
    ):
        if isinstance(projected, RowFailure):
            report.relations_skipped += 1
            _record_skipped_row(
                report, label=mapping.relation_type, source_file=mapping.source_file,
                row_number=projected.row_number, reason=projected.reason,
            )
            continue
        try:
            # 端点存在性守卫留在写入层：它需要预取的 node_key 集合，而且
            # 语义是"写入时刻这个端点在不在术语表里"，不是 projection 能
            # 回答的。守卫本身一字未改——merge_relation 的两端都是 MERGE，
            # node_key 对不上任何已有节点时不会报错，而是凭空建出一个只有
            # tenant_id/node_key 的幽灵节点。
            for key, known_keys, term_type in (
                (projected.subject_node_key, subject_node_keys, mapping.subject_term_type),
                (projected.object_node_key, object_node_keys, mapping.object_term_type),
            ):
                if key not in known_keys:
                    raise RowProcessingError(
                        f"关系端点 {key!r} 在术语表里不存在"
                        f"（{term_type!r} 的实体行可能被跳过或尚未写入）"
                    )
            await graph_client.merge_relation(
                subject_standard_name=projected.subject_node_key,
                object_standard_name=projected.object_node_key,
                relation_type=mapping.relation_type, source=mapping.source_file,
                tenant_id=tenant_id, provenance=provenance.ETL, recorded_at=recorded_at,
            )
            report.relations_written += 1
            _record_written(report, label=mapping.relation_type)
        except RowProcessingError as exc:
            report.relations_skipped += 1
            _record_skipped_row(
                report, label=mapping.relation_type, source_file=mapping.source_file,
                row_number=projected.row_number, reason=str(exc),
            )
```

import 区补上 `project_relation_rows`。

- [ ] **Step 5: 跑全量**

Expected: `1448 passed`（1446 + 2），0 failed。既有的两条幽灵节点用例（`test_run_schema_etl_relation_endpoint_never_written_is_skipped_not_ghost_merged`、`test_run_schema_etl_column_key_endpoint_never_written_is_skipped_not_ghost_merged`）必须仍然通过——它们守的就是端点守卫。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/etl_projection.py app/graphrag/schema_etl.py tests/graphrag/test_etl_projection.py
git commit -m "refactor(etl): let relation writes consume projected rows"
```

---

## Task 5: 写入前预检——主键重复整体失败、零写入

**Files:**
- Modify: `app/graphrag/schema_etl.py`（`run_schema_etl`）
- Test: `tests/graphrag/test_schema_etl.py`（**只新增**用例，既有用例一条不改）

**Interfaces:**
- Consumes: `scan_entity_node_keys(conn, *, tenant_id, mapping, data_dir) -> KeyScanResult`、`DuplicateNodeKeyError`、`format_duplicate_key_error(dict[str, dict[str, list[int]]]) -> str`（Task 2）
- Produces: `run_schema_etl` 在有重复键时抛 `DuplicateNodeKeyError`，`terms` 表和图客户端零写入。

**这是本设计的核心保证。**

**预检阶段与 mapping 级校验的关系（实施者必须想清楚这一点）：** 今天 `run_schema_etl` 里，`_write_entity_mapping` 抛 `RowProcessingError`（比如 `term_type` 不在已确认 schema 里）会被捕获、记进 `skipped_mappings`、继续跑其余 mapping。预检阶段**不做**这个校验——`scan_entity_node_keys` 不查 schema，只算键。所以：一个 `term_type` 不合法的 mapping 在预检阶段仍然会被扫描（可能白扫一遍），然后在写入阶段照旧被记进 `skipped_mappings`。这是可接受的：多读一遍文件，换来预检逻辑不必重复一遍 schema 校验。**不要**为了"优化"把 schema 校验也搬进预检——那会改变 `skipped_mappings` 的语义。

- [ ] **Step 1: 在 `tests/graphrag/test_schema_etl.py` 末尾新增三条测试**

先在文件顶部的 import 里补上：

```python
from app.graphrag.etl_projection import DuplicateNodeKeyError
from app.graphrag.etl_stable_code_registry import lookup_stable_code
```

然后在文件末尾追加：

```python
async def test_run_schema_etl_raises_on_duplicate_node_keys_and_writes_nothing(tmp_path):
    """主键重复是配置错误，不是脏数据：node_key_parts 声明的列组合不足以
    唯一标识每一行。整体失败、零写入——部分写入会留下一个"看起来成功了、
    实际缺了一部分"的图谱，比失败更难发现。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n"
        "P1,甲,M1\n"
        "P1,乙,M2\n",
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )
    graph_client = FakeGraphClient()

    with pytest.raises(DuplicateNodeKeyError) as excinfo:
        await run_schema_etl(
            conn=conn, graph_client=graph_client, config=config, data_dir=tmp_path
        )

    message = str(excinfo.value)
    assert "Product" in message
    assert "Product:P1" in message
    assert "2, 3" in message  # 冲突的源文件行号，第 1 行是表头
    assert await list_terms(conn, "muji") == []
    assert graph_client.synced == []


async def test_run_schema_etl_dirty_rows_still_skip_instead_of_failing_the_whole_run(tmp_path):
    """行级脏数据（缺列）语义不变：跳过 + 记报告，不升级成整体失败。
    只有主键重复才整体失败。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_text(
        "product_group_id,product_group_name,md_no\n"
        "P1,甲,M1\n"
        ",乙,M2\n",
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_parts=["product_group_name"],
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 1
    assert report.entities_skipped == 1
    assert len(await list_terms(conn, "muji")) == 1


async def test_run_schema_etl_reuses_stable_codes_allocated_before_a_duplicate_failure(tmp_path):
    """预检会调 compute_node_key(allow_allocation=True)，也就是说预检失败
    之前稳定码已经写进 etl_stable_code_registry 了——"零写入"的准确表述是
    "terms 和图零写入"，不是"零副作用"。这条用例钉住副作用是无害的：
    稳定码幂等分配，下次运行命中同一个码，不产生新码、不漂移。"""
    conn = await _confirmed_conn()
    (tmp_path / "variants.csv").write_text(
        "variant_value,dup_key\n"
        "红,K1\n"
        "蓝,K1\n",
        encoding="utf-8",
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="VariantValue", source_file="variants.csv",
                standard_name_parts=["variant_value"],
                node_key_parts=[ColumnNodeKeyPart(column="dup_key")],
                field_mappings={},
            ),
        ],
        relations=[],
    )

    with pytest.raises(DuplicateNodeKeyError):
        await run_schema_etl(
            conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
        )
    with pytest.raises(DuplicateNodeKeyError):
        await run_schema_etl(
            conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
        )

    # 两次运行都失败，terms 始终是空的——重复的失败不会累积出半份数据。
    assert await list_terms(conn, "muji") == []
```

**实施者注意（这条是要求，不是建议）：** 第三条用例现在用的是 `ColumnNodeKeyPart`，根本不走稳定码分配路径，因此它的名字承诺的事情它并没有验证。请把 `node_key_parts` 换成 `AllocatedCodeNodeKeyPart`，让两次运行都真的经过分配路径，并在两次之间用 `lookup_stable_code` 断言码没变：

```python
    code_after_first = await lookup_stable_code(
        conn, tenant_id="muji", scope=..., raw_value="红"
    )
    # ...第二次运行之后...
    assert await lookup_stable_code(
        conn, tenant_id="muji", scope=..., raw_value="红"
    ) == code_after_first
```

`scope` 的拼接规则见 `compute_node_key`：`":".join([term_type, *[row[c] for c in part.scope_columns]])`。`AllocatedCodeNodeKeyPart` 的确切字段名请打开 `schema_etl_config.py` 核对。保留"两次都抛 `DuplicateNodeKeyError` 且 `list_terms` 始终为空"的断言。

- [ ] **Step 2: 跑新测试确认失败**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest tests/graphrag/test_schema_etl.py -q -k "duplicate or dirty_rows_still_skip or stable_codes_allocated" > /tmp/o.txt 2>&1
grep -E "passed|failed|Error" /tmp/o.txt | tail -3
```

Expected: 第一条和第三条 FAIL（现在不会抛 `DuplicateNodeKeyError`，而是静默取最后一行写进去），第二条 PASS（行级跳过的语义本来就对）。

- [ ] **Step 3: 在 `run_schema_etl` 里加入预检阶段**

在 `schema_etl.py` 的 import 区加入：

```python
from app.graphrag.etl_projection import (
    DuplicateNodeKeyError,
    format_duplicate_key_error,
    scan_entity_node_keys,
)
```

在 `run_schema_etl` 里，`await ensure_stable_code_registry_schema(conn)` 之后、`for entity_mapping in config.entities:` 写入循环**之前**，插入：

```python
    # 预检：所有实体映射先各扫一遍键，确认没有重复，才进入写入。
    #
    # 为什么整体失败而不是逐行跳过：主键重复意味着这份配置的 node_key_parts
    # 选错了——它没能唯一标识每一行。这不是"某几行数据脏"，跳过多少行都不
    # 会让配置变对。部分写入反而留下一个"看起来成功了、实际缺了一部分"的
    # 图谱，比失败更难发现。见 2026-08-30-etl-layered-pipeline-design.md。
    #
    # 这一遍不做 term_type 的 schema 校验——那件事仍然由 _write_entity_mapping
    # 负责，失败仍然记进 skipped_mappings。预检只关心键。
    duplicates_by_term_type: dict[str, dict[str, list[int]]] = {}
    for entity_mapping in config.entities:
        try:
            scan = await scan_entity_node_keys(
                conn, tenant_id=config.tenant_id, mapping=entity_mapping, data_dir=data_dir,
            )
        except RowProcessingError:
            # 文件类型不支持之类的问题，留给写入阶段按老路径记进
            # skipped_mappings，预检不抢着报错。
            continue
        if scan.duplicate_keys:
            duplicates_by_term_type[entity_mapping.term_type] = scan.duplicate_keys
    if duplicates_by_term_type:
        raise DuplicateNodeKeyError(format_duplicate_key_error(duplicates_by_term_type))
```

- [ ] **Step 4: 跑新测试确认通过**

```bash
PYTHONPATH="D:/project/customer_rag" PYTHONIOENCODING=utf-8 timeout 400 python -m pytest tests/graphrag/test_schema_etl.py -q -k "duplicate or dirty_rows_still_skip or stable_codes_allocated" > /tmp/o.txt 2>&1
grep -E "passed|failed" /tmp/o.txt | tail -2
```

Expected: `3 passed`。

- [ ] **Step 5: 跑全量**

Expected: `1451 passed`（1448 + 3），0 failed。

**如果有既有用例开始失败**：很可能是某条老用例的夹具本来就带重复键（以前靠"静默取最后一行"通过）。这种情况**不要**放宽预检——那正是本设计要消灭的静默行为。改夹具让键唯一，并在报告里说明改了哪条、为什么原来的夹具键是重复的。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/schema_etl.py tests/graphrag/test_schema_etl.py
git commit -m "feat(etl): fail the whole run on duplicate node keys, before any write"
```

---

## Self-Review

**1. Spec coverage**

| spec 章节 | 对应任务 |
|---|---|
| 第一层 · staging | Task 1 |
| 第二层 · projection（实体） | Task 2 |
| 一份文件背书多个对象类型，靠 projection 切开 | Task 2（每个 `EntityMapping` 各跑一次 projection，天然是每个 term_type 一份） |
| 第三层 · 写入（实体） | Task 3 |
| 第三层 · 写入（关系） | Task 4 |
| 主键重复：整体失败 | Task 5 |
| `DuplicateNodeKeyError` 消息格式（20 条样例上限 + 总数） | Task 2 的 `format_duplicate_key_error` |
| `allocated_code` 副作用要用测试钉住 | Task 5 Step 1 第三条用例 |
| `ON CONFLICT DO UPDATE` 语义澄清 | 无任务——spec 明说"这一条是澄清，不是改动" |
| 报告结构 / 管理后台同步 | **无任务**——见本计划开头"修正一"，实读代码后确认不需要 |
| 内存取舍 | 已定：两遍读文件、只驻留键。Task 2 的接口就是这个决定的产物 |

**2. Placeholder scan**：无 TBD/TODO；每个代码步骤都给了可直接粘贴的完整代码。两处要求实施者去核对 `schema_etl_config.py` 实际字段名的地方（Task 4 Step 1、Task 5 Step 1）是刻意的——那是我没有实读到的细节，与其编一个可能错的字段名，不如明确要求核对并在报告里说明。

**3. Type consistency**：`ProjectedRow` / `RowFailure` / `KeyScanResult` / `ProjectedRelationRow` 四个数据类在 Task 2、Task 3、Task 4、Task 5 里的字段名和用法一致；`read_table_rows`（Task 1 产出）在 Task 2、Task 4 被消费，名字一致；`scan_entity_node_keys` 的返回类型 `KeyScanResult` 在 Task 5 里按 `.duplicate_keys` 消费，与 Task 2 的定义一致；`format_duplicate_key_error` 的入参 `dict[str, dict[str, list[int]]]` 与 Task 5 里构造的 `duplicates_by_term_type` 类型一致。

**4. 测试计数链**：1433（基线）→ 1440（Task 1，+7）→ 1446（Task 2，+6）→ 1446（Task 3，+0，纯重构）→ 1448（Task 4，+2）→ 1451（Task 5，+3）。
