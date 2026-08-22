# Schema ETL 多格式数据文件上传 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 「表格数据 ETL 上传」功能从只支持 CSV 扩展到支持 XLSX/XLS/TSV，前后端各自把"读取源文件"这一步从 CSV 专属实现改造成按扩展名分流的通用实现，其余的 schema 映射/转换逻辑保持不变。

**Architecture:** 后端把 `app/graphrag/schema_etl.py` 里唯一的 `_read_csv_rows` 替换成按扩展名分流的 `_read_table_rows`（CSV/TSV 走 `csv.DictReader` + 编码探测，XLSX 走 `openpyxl` 流式读，XLS 走 `xlrd`），单元格类型→字符串的转换规则单独抽成 `schema_etl_row_processing.py` 里一个纯函数（`convert_excel_cell_to_string`），跟现有 `convert_field_value` 放在一起、走同样的直接单元测试路线。上传 endpoint 新增扩展名白名单校验。前端把 `csvHeader.ts` 改造成 `tableHeader.ts`，同样按扩展名分流，XLSX/XLS 走 SheetJS 的 `xlsx` 包读第一行。

**Tech Stack:** Python 3.12 / FastAPI / `openpyxl`（读 XLSX）/ `xlrd`（读 XLS）/ `xlwt`（仅测试夹具生成，dev-only）/ pytest；前端 TypeScript / React / SheetJS `xlsx` 包。

**Spec:** `docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md`

## Global Constraints

- 支持的数据文件格式：`.csv`（已有）、`.tsv`、`.xlsx`、`.xls`。不做 JSON/ODS。
- Excel 文件固定读第一个工作表，其余 sheet 忽略，不提示。
- 单元格类型 → 字符串转换规则（决策 3，逐条对照）：
  - `int` → `str(value)`
  - `float` 且值等于其整数部分 → 转 `int` 再 `str()`，去掉尾随 `.0`
  - `float` 且有小数部分 → `str(value)`，不额外补零/截断
  - `datetime.datetime` 且时:分:秒:微秒全为 0（纯日期单元格）→ `strftime("%Y-%m-%d")`
  - `datetime.datetime` 有非零时间部分 → `strftime("%Y-%m-%d %H:%M:%S")`
  - `datetime.date`（非 datetime）→ `strftime("%Y-%m-%d")`
  - `bool` → `str(value)`（`"True"`/`"False"`）
  - `None` / 空单元格 → `""`
  - `str` → 原样返回（可以 `.strip()`）
  - **实现顺序要求**：`bool` 的 `isinstance` 检查必须排在 `int` 之前（`bool` 是 `int` 的子类）；`datetime` 的检查必须排在 `date` 之前（`datetime` 是 `date` 的子类）。顺序反了会导致类型判断分支永远走不到正确分支。
- 后端上传 endpoint 新增扩展名白名单校验：只接受 `.csv`/`.tsv`/`.xlsx`/`.xls`（大小写不敏感），不在白名单里的文件在写盘之前直接 `400 Bad Request`，`detail` 格式：`f"{filename!r}: 不支持的文件类型，只支持 {allowed}"`，`allowed` 是白名单扩展名用 `/` 连接的字符串。校验失败要清理已创建的 `run_dir`（`shutil.rmtree(run_dir, ignore_errors=True)`），跟本文件其它 400 分支的清理方式一致。
- CSV/TSV 编码探测：先尝试 UTF-8 严格解码，失败则回退尝试 GBK；两者都失败则让 GBK 阶段的 `UnicodeDecodeError` 原样往上抛，不做进一步猜测。探测只在读取开始前做一次（读一次原始字节做 decode 测试），探测完成后用确定的编码重新以文本模式打开文件，交给 `csv.DictReader` 流式处理——不把解码后的全文一次性留在内存里。
- 前端 XLSX/XLS 解析库：SheetJS 的 `xlsx` 包，`^0.18.5`（执行时允许安装当时 npm 上的最新稳定版，不要求精确匹配这个版本号）。
- 项目校验方式：后端 `pytest`；前端 `cd frontend && npx tsc --noEmit`。前端这个目录目前没有自动化测试框架覆盖（`csvHeader.ts` 之前也没有测试），本计划不强制新增前端测试。
- 依赖安装方式：`.venv/Scripts/python.exe -m pip install -e ".[dev]"`（项目用 `pyproject.toml` 的可编辑安装，新增依赖后要重新跑一次这个命令让新依赖生效）；前端 `cd frontend && npm install`。

---

### Task 1: 后端新增依赖（openpyxl / xlrd / xlwt）

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces：`openpyxl`/`xlrd` 在生产依赖里可用；`xlwt` 在 dev 依赖里可用（仅供 Task 4 的测试夹具生成使用，生产代码不依赖它）

- [ ] **Step 1: 修改 `pyproject.toml`**

当前 `dependencies` 列表结尾（已知，供比对）：

```toml
dependencies = [
    "httpx>=0.28",
    "fastapi>=0.141",
    "uvicorn>=0.30",
    "pydantic-settings>=2.14",
    "pymilvus>=3.0",
    "pypdf>=6.0",
    "neo4j>=6.0",
    "pyyaml>=6.0",
    "langgraph>=1.2",
    "aiosqlite>=0.22",
    "python-multipart>=0.0.20",
    "python-docx>=1.2",
    "pytesseract>=0.3",
    "Pillow>=10.0",
    "PyMuPDF>=1.24",
    "dashscope>=1.20",
]
```

改成（在末尾追加两行）：

```toml
dependencies = [
    "httpx>=0.28",
    "fastapi>=0.141",
    "uvicorn>=0.30",
    "pydantic-settings>=2.14",
    "pymilvus>=3.0",
    "pypdf>=6.0",
    "neo4j>=6.0",
    "pyyaml>=6.0",
    "langgraph>=1.2",
    "aiosqlite>=0.22",
    "python-multipart>=0.0.20",
    "python-docx>=1.2",
    "pytesseract>=0.3",
    "Pillow>=10.0",
    "PyMuPDF>=1.24",
    "dashscope>=1.20",
    "openpyxl>=3.1",
    "xlrd>=2.0",
]
```

当前 `[project.optional-dependencies]` 的 `dev` 组（已知，供比对）：

```toml
[project.optional-dependencies]
dev = [
    "pytest>=9.1",
    "pytest-asyncio>=1.4",
    "reportlab>=5.0",
]
```

改成：

```toml
[project.optional-dependencies]
dev = [
    "pytest>=9.1",
    "pytest-asyncio>=1.4",
    "reportlab>=5.0",
    "xlwt>=1.3",
]
```

`xlwt` 只用来在测试里生成 `.xls` 格式的夹具文件（`xlrd` 只能读 `.xls`、不能写），跟 `reportlab` 只用来在测试里生成 PDF 夹具是同一个模式（见 `tests/ingestion/test_ingest_pdf.py`），生产代码不导入 `xlwt`。

- [ ] **Step 2: 安装依赖**

Run: `.venv/Scripts/python.exe -m pip install -e ".[dev]"`
Expected: 安装成功，输出里能看到 `openpyxl`/`xlrd`/`xlwt` 被安装或已满足

- [ ] **Step 3: 验证三个库都能正常 import**

Run: `.venv/Scripts/python.exe -c "import openpyxl, xlrd, xlwt; print(openpyxl.__version__, xlrd.__version__, xlwt.__version__)"`
Expected: 打印三个版本号，无报错

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore(schema-etl): add openpyxl/xlrd/xlwt dependencies for multi-format upload"
```

---

### Task 2: 单元格类型 → 字符串转换纯函数

**Files:**
- Modify: `app/graphrag/schema_etl_row_processing.py`
- Test: `tests/graphrag/test_schema_etl_row_processing.py`

**Interfaces:**
- Consumes：无（纯函数，不依赖任何数据库/网络）
- Produces：`convert_excel_cell_to_string(value: object) -> str`，供 Task 4 的 `_read_xlsx_rows`/`_read_xls_rows` 调用

- [ ] **Step 1: 读取当前 `app/graphrag/schema_etl_row_processing.py` 开头的 import 块**

当前内容（已知，供比对）：

```python
from __future__ import annotations

from app.graphrag.etl_stable_code_registry import allocate_stable_code, lookup_stable_code
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import AllocatedCodeNodeKeyPart, ColumnNodeKeyPart

import aiosqlite
```

改成（新增 `datetime` 的 `date`/`datetime` 导入）：

```python
from __future__ import annotations

from datetime import date, datetime

from app.graphrag.etl_stable_code_registry import allocate_stable_code, lookup_stable_code
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import AllocatedCodeNodeKeyPart, ColumnNodeKeyPart

import aiosqlite
```

- [ ] **Step 2: 写失败的测试**

在 `tests/graphrag/test_schema_etl_row_processing.py` 文件末尾追加（文件顶部已有 `pytestmark = pytest.mark.anyio`，这批新测试是纯同步函数，不需要 async，不受这个 pytestmark 影响——现有文件里 `convert_field_value` 相关的测试也是同步的，混在同一个文件里没问题）：

```python
from datetime import date, datetime

from app.graphrag.schema_etl_row_processing import convert_excel_cell_to_string


def test_convert_excel_cell_to_string_int():
    assert convert_excel_cell_to_string(123) == "123"


def test_convert_excel_cell_to_string_float_integer_value_drops_trailing_zero():
    assert convert_excel_cell_to_string(123.0) == "123"


def test_convert_excel_cell_to_string_float_with_decimal_part():
    assert convert_excel_cell_to_string(123.45) == "123.45"


def test_convert_excel_cell_to_string_datetime_with_time_part():
    assert convert_excel_cell_to_string(datetime(2026, 8, 21, 14, 30, 0)) == "2026-08-21 14:30:00"


def test_convert_excel_cell_to_string_datetime_at_midnight_formats_as_date_only():
    assert convert_excel_cell_to_string(datetime(2026, 8, 21, 0, 0, 0)) == "2026-08-21"


def test_convert_excel_cell_to_string_date_object():
    assert convert_excel_cell_to_string(date(2026, 8, 21)) == "2026-08-21"


def test_convert_excel_cell_to_string_bool_true():
    assert convert_excel_cell_to_string(True) == "True"


def test_convert_excel_cell_to_string_bool_false():
    assert convert_excel_cell_to_string(False) == "False"


def test_convert_excel_cell_to_string_none_is_empty_string():
    assert convert_excel_cell_to_string(None) == ""


def test_convert_excel_cell_to_string_string_value_is_stripped():
    assert convert_excel_cell_to_string("  圆角收纳盒  ") == "圆角收纳盒"


def test_convert_excel_cell_to_string_bool_is_not_caught_by_int_branch():
    """回归测试：bool 是 int 的子类，如果 isinstance(value, int) 的判断排在
    isinstance(value, bool) 前面，True 会被 int 分支吞掉变成 "1" 而不是
    "True"——这条测试专门守住分支顺序，不依赖上面两条 bool 测试的巧合。"""
    result = convert_excel_cell_to_string(True)
    assert result == "True"
    assert result != "1"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl_row_processing.py -v -k convert_excel_cell_to_string`
Expected: 全部 FAIL，报 `ImportError: cannot import name 'convert_excel_cell_to_string'`

- [ ] **Step 4: 实现 `convert_excel_cell_to_string`**

在 `app/graphrag/schema_etl_row_processing.py` 里 `convert_field_value` 函数**之后**（文件末尾）追加：

```python
def convert_excel_cell_to_string(value: object) -> str:
    """把 Excel 单元格的原生值（openpyxl/xlrd 读出来的 int/float/str/bool/
    datetime/date/None）转换成字符串，跟 CSV 场景里"一行是 dict[str, str]"
    的契约对齐——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md
    决策 3 的转换规则表。

    分支顺序不能打乱：bool 是 int 的子类（isinstance(True, int) 也是
    True），bool 分支必须排在 int 分支前面；datetime 是 date 的子类，
    datetime 分支必须排在 date 分支前面。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl_row_processing.py -v -k convert_excel_cell_to_string`
Expected: 全部 PASS（12 个用例）

- [ ] **Step 6: 跑整个测试文件确认没有破坏已有测试**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl_row_processing.py -v`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add app/graphrag/schema_etl_row_processing.py tests/graphrag/test_schema_etl_row_processing.py
git commit -m "feat(schema-etl): add convert_excel_cell_to_string for Excel cell type conversion"
```

---

### Task 3: CSV/TSV 编码探测 + 通用分隔符读取器

**Files:**
- Modify: `app/graphrag/schema_etl.py`
- Test: `tests/graphrag/test_schema_etl.py`

**Interfaces:**
- Consumes：无
- Produces：`_detect_text_encoding(path: Path) -> str`、`_read_delimited_rows(path: Path, *, delimiter: str) -> Iterator[dict[str, str]]`（这两个是私有函数，不对外导出，Task 4 会在同一个文件里的 `_read_table_rows` 分流函数里调用）。这一步先删掉 `_read_csv_rows`，`_write_entity_mapping`/`_write_relation_mapping` 暂时改成直接调用 `_read_delimited_rows(path, delimiter=",")`（Task 4 会再把调用点改成走 `_read_table_rows` 分流）。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_schema_etl.py` 文件末尾追加（文件顶部已有的 import 和 `_confirmed_conn`/`FakeGraphClient` 直接复用，不用重复定义）：

```python
async def test_run_schema_etl_reads_gbk_encoded_csv(tmp_path):
    """国内 Excel 导出 CSV 常见默认编码是 GBK，不是 UTF-8——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md
    决策 6。这里直接写 GBK 编码的字节，不依赖任何自动转码工具，验证读取器
    自己能探测出编码并正确解码出中文列名/值。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.csv").write_bytes(
        "product_group_id,product_group_name,md_no\n1001,圆角收纳盒,A123\n".encode("gbk")
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.csv",
                standard_name_column="product_group_name",
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
    assert report.entities_skipped == 0
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"


async def test_run_schema_etl_reads_tsv_source_file(tmp_path):
    """TSV 只是分隔符从逗号换成制表符，验证扩展名 .tsv 能被正确识别并用
    制表符分隔解析——见决策 1。"""
    conn = await _confirmed_conn()
    (tmp_path / "products.tsv").write_text(
        "product_group_id\tproduct_group_name\tmd_no\n1001\t圆角收纳盒\tA123\n", encoding="utf-8"
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.tsv",
                standard_name_column="product_group_name",
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
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"
```

这两个测试要用到 `get_term`——检查文件顶部 import 是否已有它（现有 `from app.graphrag.terms_store import ensure_terms_schema, get_term` 这一行已经导入了，不用新增）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl.py -v -k "gbk_encoded or tsv_source_file"`
Expected: `test_run_schema_etl_reads_gbk_encoded_csv` FAIL（`UnicodeDecodeError`，因为当前硬编码 UTF-8），`test_run_schema_etl_reads_tsv_source_file` FAIL（`.tsv` 文件当前会被当成没有任何行的空 CSV，或者读取器压根不认识这个后缀——具体报错以实际跑出来的为准，只要是 FAIL 就说明现状确实不支持，符合预期）

- [ ] **Step 3: 实现编码探测 + 通用分隔符读取器**

读取当前 `app/graphrag/schema_etl.py` 第 1-27 行的 import 块（已知，供比对）：

```python
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

import aiosqlite

from app.config.settings import Settings
from app.graphrag import provenance
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.review_factory import build_review_conn_from_settings
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping, SchemaETLConfig, load_schema_etl_config
from app.graphrag.schema_etl_row_processing import RowProcessingError, compute_node_key, convert_field_value
from app.graphrag.terms_store import TermNameConflictError, UnknownCategoryError, upsert_term_with_node_key
```

不需要改动这个 import 块（Task 4 才会新增 `openpyxl`/`xlrd`/`convert_excel_cell_to_string` 的 import）。

把第 61-65 行的 `_read_csv_rows`：

```python
def _read_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    """逐行流式产出源文件的行，不把整个文件读进内存——设计文档第 6.4 节
    给出的真实规模是"MUJI 一张 SKU 表 18 万+ 行"。"""
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)
```

替换成：

```python
def _detect_text_encoding(path: Path) -> str:
    """CSV/TSV 源文件的编码探测：优先按 UTF-8 严格解码，失败则回退尝试
    GBK（国内 Excel 导出 CSV 最常见的默认编码）——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md
    决策 6。这里读一遍原始字节只是为了做 decode 测试，不保留解码结果；
    真正的行级处理仍然通过 csv.DictReader 用确定的编码重新打开文件、
    流式进行，不会把整份解码后的文本一次性留在内存里。两种编码都解码
    失败时，让 GBK 阶段的 UnicodeDecodeError 原样往上抛，不做进一步猜测。
    """
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    raw.decode("gbk")
    return "gbk"


def _read_delimited_rows(path: Path, *, delimiter: str) -> Iterator[dict[str, str]]:
    """逐行流式产出 CSV/TSV 源文件的行，不把整个文件读进内存——设计文档第
    6.4 节给出的真实规模是"MUJI 一张 SKU 表 18 万+ 行"。"""
    encoding = _detect_text_encoding(path)
    with path.open(encoding=encoding, newline="") as handle:
        yield from csv.DictReader(handle, delimiter=delimiter)
```

把 `_write_entity_mapping` 里第 96 行：

```python
    for row_number, row in enumerate(_read_csv_rows(data_dir / mapping.source_file), start=2):  # 第 1 行是表头
```

改成：

```python
    for row_number, row in enumerate(_read_delimited_rows(data_dir / mapping.source_file, delimiter=","), start=2):  # 第 1 行是表头
```

把 `_write_relation_mapping` 里第 168 行：

```python
    for row_number, row in enumerate(_read_csv_rows(data_dir / mapping.source_file), start=2):
```

改成：

```python
    for row_number, row in enumerate(_read_delimited_rows(data_dir / mapping.source_file, delimiter=","), start=2):
```

这一步先把两个调用点硬编码成 `delimiter=","`（等价于原来的行为，先只解决"编码探测"这一件事），Task 4 会把这两处再改一次，改成调用按扩展名分流的 `_read_table_rows`（那时候 `.tsv` 才会真正走 `delimiter="\t"`）。这一步跑完之后，`test_run_schema_etl_reads_tsv_source_file` 预期仍然是 FAIL 的（`.tsv` 文件此时还是被当逗号分隔解析），这是正常的中间状态，Task 4 才会让它转 PASS。

- [ ] **Step 4: 跑测试确认 GBK 用例通过、TSV 用例仍然失败（预期中的中间状态）**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl.py -v -k "gbk_encoded or tsv_source_file"`
Expected: `test_run_schema_etl_reads_gbk_encoded_csv` PASS；`test_run_schema_etl_reads_tsv_source_file` 仍然 FAIL（预期中，Task 4 才会修）

- [ ] **Step 5: 跑整个测试文件确认没有破坏已有的 CSV 测试**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl.py -v -k "not tsv_source_file"`
Expected: 除了刚才特意排除的 TSV 用例，其余全部 PASS

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/schema_etl.py tests/graphrag/test_schema_etl.py
git commit -m "feat(schema-etl): add UTF-8/GBK encoding fallback for CSV/TSV source files"
```

---

### Task 4: XLSX/XLS 读取器 + 按扩展名分流

**Files:**
- Modify: `app/graphrag/schema_etl.py`
- Test: `tests/graphrag/test_schema_etl.py`

**Interfaces:**
- Consumes：Task 2 的 `convert_excel_cell_to_string(value: object) -> str`；Task 3 的 `_read_delimited_rows(path: Path, *, delimiter: str) -> Iterator[dict[str, str]]`
- Produces：`_read_table_rows(path: Path) -> Iterator[dict[str, str]]`（按扩展名分流的统一入口，`_write_entity_mapping`/`_write_relation_mapping` 改成调用这个）

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_schema_etl.py` 文件末尾追加：

```python
def _write_xlsx_fixture(path, *, header: list[str], rows: list[list[object]]) -> None:
    """用 openpyxl 生成一个最小的 xlsx 测试夹具文件——openpyxl 既能读也能
    写，不需要额外引入别的库。"""
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(header)
    for row in rows:
        worksheet.append(row)
    workbook.save(str(path))


def _write_xls_fixture(path, *, header: list[str], rows: list[list[object]]) -> None:
    """用 xlwt 生成一个最小的 xls（旧版二进制 Excel）测试夹具文件——xlrd
    只能读 xls 不能写，xlwt 只能写 xls 不能写 xlsx，两个库分工明确，这里
    只用来造测试数据，生产代码不导入 xlwt。"""
    import xlwt

    workbook = xlwt.Workbook()
    worksheet = workbook.add_sheet("Sheet1")
    for col, name in enumerate(header):
        worksheet.write(0, col, name)
    for row_idx, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            worksheet.write(row_idx, col, value)
    workbook.save(str(path))


async def test_run_schema_etl_reads_xlsx_source_file(tmp_path):
    conn = await _confirmed_conn()
    _write_xlsx_fixture(
        tmp_path / "products.xlsx",
        header=["product_group_id", "product_group_name", "md_no"],
        rows=[[1001, "圆角收纳盒", "A123"]],
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.xlsx",
                standard_name_column="product_group_name",
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
    assert report.entities_skipped == 0
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"
    # xlsx 单元格里 1001 是原生 int，node_key 必须是 "Product:1001" 而不是
    # "Product:1001.0"——验证 convert_excel_cell_to_string 真的被用在了
    # 读取路径上，不是只在 Task 2 的单元测试里孤立存在。


async def test_run_schema_etl_reads_xls_source_file(tmp_path):
    conn = await _confirmed_conn()
    _write_xls_fixture(
        tmp_path / "products.xls",
        header=["product_group_id", "product_group_name", "md_no"],
        rows=[[1001, "圆角收纳盒", "A123"]],
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.xls",
                standard_name_column="product_group_name",
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
    term = await get_term(conn, tenant_id="muji", standard_name="圆角收纳盒")
    assert term is not None
    assert term.node_key == "Product:1001"


async def test_run_schema_etl_xlsx_empty_sheet_writes_nothing(tmp_path):
    """只有表头没有数据行的 xlsx，不应该报错，也不应该写入任何实体——跟
    CSV 场景下"只有表头"的行为一致。"""
    conn = await _confirmed_conn()
    _write_xlsx_fixture(
        tmp_path / "products.xlsx",
        header=["product_group_id", "product_group_name", "md_no"],
        rows=[],
    )
    config = SchemaETLConfig(
        tenant_id="muji",
        entities=[
            EntityMapping(
                term_type="Product", source_file="products.xlsx",
                standard_name_column="product_group_name",
                node_key_parts=[ColumnNodeKeyPart(column="product_group_id")],
                field_mappings={"md_no": "md_no"},
            ),
        ],
        relations=[],
    )

    report = await run_schema_etl(
        conn=conn, graph_client=FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 0
    assert report.entities_skipped == 0
```

注意这批测试**不需要**重复写 `test_run_schema_etl_reads_tsv_source_file`——Task 3 已经写过了，这一步跑完之后那条测试会从 FAIL 转 PASS（因为 `_read_table_rows` 分流之后 `.tsv` 才会真正走 `delimiter="\t"`）。

- [ ] **Step 2: 跑测试确认新用例失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl.py -v -k "xlsx_source_file or xls_source_file or xlsx_empty_sheet"`
Expected: 全部 FAIL（`_read_table_rows` 还不存在，或者 `.xlsx`/`.xls` 文件被当逗号分隔文本读，读出乱码/报错，具体报错以实际跑出来的为准）

- [ ] **Step 3: 实现 XLSX/XLS 读取器 + 分流函数**

在 `app/graphrag/schema_etl.py` 顶部 import 块，把：

```python
from app.graphrag.schema_etl_row_processing import RowProcessingError, compute_node_key, convert_field_value
```

改成：

```python
from app.graphrag.schema_etl_row_processing import (
    RowProcessingError,
    compute_node_key,
    convert_excel_cell_to_string,
    convert_field_value,
)
```

并在文件顶部 `import csv` 之后新增：

```python
import xlrd
from openpyxl import load_workbook
```

在 `_read_delimited_rows` 函数（Task 3 新增的）**之后**追加：

```python
def _read_xlsx_rows(path: Path) -> Iterator[dict[str, str]]:
    """流式读取 xlsx 第一个工作表，第一行是表头——见决策 2（固定读第一个
    sheet）。read_only=True 让 openpyxl 用懒加载模式逐行产出，不把整个
    工作表读进内存，跟 CSV 路径同一个"18 万+ 行不能爆内存"的约束。
    data_only=True 拿单元格公式算出来的值，不拿公式字符串本身。"""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook.worksheets[0]
        rows_iter = worksheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return
        header = [str(cell).strip() if cell is not None else "" for cell in header_row]
        for row in rows_iter:
            yield {
                header[i]: convert_excel_cell_to_string(row[i] if i < len(row) else None)
                for i in range(len(header))
            }
    finally:
        workbook.close()


def _xlrd_cell_to_python_value(cell: "xlrd.sheet.Cell", datemode: int) -> object:
    """把 xlrd 的 Cell（用 ctype 标记类型、日期存成 Excel 序列号）归一化成
    openpyxl 风格的原生 Python 值（int/float/str/bool/datetime/None），
    这样就能复用同一个 convert_excel_cell_to_string 做字符串化，不用给
    xlrd 单独写一套转换规则。"""
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, datemode)
    return cell.value  # XL_CELL_NUMBER（float）/ XL_CELL_TEXT（str）/ XL_CELL_ERROR


def _read_xls_rows(path: Path) -> Iterator[dict[str, str]]:
    """读取旧版二进制 xls 第一个工作表，第一行是表头。xlrd 没有 openpyxl
    那种懒加载流式模式，会把整个工作表读进内存——xls 是被淘汰的旧格式，
    体量通常不大，这里不为了流式特意做额外处理。"""
    workbook = xlrd.open_workbook(str(path))
    worksheet = workbook.sheet_by_index(0)
    if worksheet.nrows == 0:
        return
    header = [str(worksheet.cell_value(0, col)).strip() for col in range(worksheet.ncols)]
    for row_idx in range(1, worksheet.nrows):
        yield {
            header[col_idx]: convert_excel_cell_to_string(
                _xlrd_cell_to_python_value(worksheet.cell(row_idx, col_idx), workbook.datemode)
            )
            for col_idx in range(len(header))
        }


def _read_table_rows(path: Path) -> Iterator[dict[str, str]]:
    """按扩展名分流到对应的行读取器，统一产出 dict[str, str]——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md。"""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        yield from _read_delimited_rows(path, delimiter=",")
    elif suffix == ".tsv":
        yield from _read_delimited_rows(path, delimiter="\t")
    elif suffix == ".xlsx":
        yield from _read_xlsx_rows(path)
    elif suffix == ".xls":
        yield from _read_xls_rows(path)
    else:
        raise RowProcessingError(f"不支持的数据文件类型: {suffix!r}（{path.name}）")
```

把 `_write_entity_mapping` 里（Task 3 改过的那一行）：

```python
    for row_number, row in enumerate(_read_delimited_rows(data_dir / mapping.source_file, delimiter=","), start=2):  # 第 1 行是表头
```

改成：

```python
    for row_number, row in enumerate(_read_table_rows(data_dir / mapping.source_file), start=2):  # 第 1 行是表头
```

把 `_write_relation_mapping` 里（Task 3 改过的那一行）：

```python
    for row_number, row in enumerate(_read_delimited_rows(data_dir / mapping.source_file, delimiter=","), start=2):
```

改成：

```python
    for row_number, row in enumerate(_read_table_rows(data_dir / mapping.source_file), start=2):
```

- [ ] **Step 4: 跑测试确认全部通过（含 Task 3 遗留的 TSV 用例）**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_schema_etl.py -v`
Expected: 全部 PASS，包括这一步新增的 4 个用例和 Task 3 遗留的 `test_run_schema_etl_reads_tsv_source_file`

- [ ] **Step 5: 跑一次全量后端测试确认没有波及其它模块**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 全部 PASS（如果有跟本次改动无关的既有失败用例，如实记录下来，不要因为这个卡住，但要在报告里说明是不是本次改动引入的）

- [ ] **Step 6: Commit**

```bash
git add app/graphrag/schema_etl.py tests/graphrag/test_schema_etl.py
git commit -m "feat(schema-etl): support XLSX/XLS source files via extension dispatch"
```

---

### Task 5: 上传 endpoint 扩展名白名单校验

**Files:**
- Modify: `app/api/admin_schema_etl_routes.py`
- Test: `tests/api/test_admin_schema_etl_routes.py`

**Interfaces:**
- Consumes：无
- Produces：`start_schema_etl_run` 对 `data_files` 新增扩展名校验，不满足直接 `400`

- [ ] **Step 1: 写失败的测试**

在 `tests/api/test_admin_schema_etl_routes.py` 文件末尾追加：

```python
def test_start_run_rejects_unsupported_data_file_extension(client, review_conn):
    asyncio.run(_confirm_muji_schema(review_conn))
    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("report.pdf", b"%PDF-1.4 fake pdf content")),
    ]

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 400
    assert "report.pdf" in response.json()["detail"]
    assert "不支持的文件类型" in response.json()["detail"]


def test_start_run_rejects_unsupported_extension_cleans_up_run_dir(client, review_conn, tmp_path):
    """扩展名校验失败要清理已经创建的 run_dir，不能在磁盘上留下半成品目录
    ——跟本文件其它 400 分支（tenant_id 不一致、配置解析失败）的清理方式
    保持一致。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    tenant_dir = tmp_path / "uploads" / "schema-etl" / "muji"
    before = sorted(p.name for p in tenant_dir.iterdir()) if tenant_dir.exists() else []
    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("report.pdf", b"%PDF-1.4 fake pdf content")),
    ]

    client.post("/api/admin/muji/schema-etl/runs", files=files)

    after = sorted(p.name for p in tenant_dir.iterdir()) if tenant_dir.exists() else []
    assert after == before, "校验失败后不应该在 tenant 目录下留下新的 run_id 目录"


def test_start_run_accepts_xlsx_data_file(client, review_conn):
    """扩展名白名单要放行 xlsx，不能因为加了白名单反而把新支持的格式也
    挡在外面。"""
    asyncio.run(_confirm_muji_schema(review_conn))
    files = [
        ("config", ("config.yaml", b"tenant_id: muji\nentities: []\nrelations: []\n")),
        ("data_files", ("products.xlsx", b"PK\x03\x04fake xlsx bytes")),
    ]

    response = client.post("/api/admin/muji/schema-etl/runs", files=files)

    assert response.status_code == 200
```

`test_start_run_accepts_xlsx_data_file` 用的是假的 xlsx 字节内容（不是真实可解析的 xlsx 文件）——这条测试只验证 endpoint 层的扩展名放行逻辑，不验证文件内容能不能被真正解析（那是 Task 4 已经用真实 openpyxl 生成的夹具测过的），文件写盘之后交给后台任务处理，跟现有 `test_start_run_...` 系列测试同样只断言 `response.status_code == 200`、不等后台任务跑完的风格一致。

- [ ] **Step 2: 跑测试确认新用例失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_schema_etl_routes.py -v -k "unsupported_data_file_extension or cleans_up_run_dir or accepts_xlsx_data_file"`
Expected: `rejects_unsupported_data_file_extension`/`cleans_up_run_dir` 两条 FAIL（现在返回 200，不是预期的 400）；`accepts_xlsx_data_file` 应该已经 PASS（现状本来就什么都收）——这条先跑来确认当前行为，加完白名单后要保证它继续 PASS

- [ ] **Step 3: 实现扩展名白名单校验**

读取当前 `app/api/admin_schema_etl_routes.py` 第 55-68 行（已知，供比对）：

```python
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def _sanitize_data_filename(filename: str) -> str:
    sanitized = _UNSAFE_NAME_CHARS.sub("_", filename) or "unnamed"
    # 上面的正则只剥掉路径分隔符等危险字符，"." 和 ".." 本身不含任何被剥掉的
    # 字符，会原样穿透——拼进 run_dir / sanitized 之后分别指向 run_dir 自己
    # 和它的父目录（父目录必然已存在，是 run_dir.mkdir(parents=True) 顺带建
    # 出来的），对着一个已存在的目录 write_bytes() 会抛 IsADirectoryError，
    # 不是"逃出 run_dir"式的任意路径穿越，但仍然违反了本函数自己的契约
    # （防止用文件名逃出 run_dir），必须单独兜底这两个纯点的特殊值。
    if sanitized in (".", ".."):
        sanitized = f"_{sanitized}"
    return sanitized
```

在这个函数**之后**追加：

```python
_ALLOWED_DATA_FILE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls"}


def _validate_data_file_extensions(data_files: list[UploadFile]) -> None:
    """上传的数据文件必须是白名单里的格式——见
    docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md
    决策 4。在文件写盘之前做，不满足直接 400，不留下垃圾文件。"""
    for data_file in data_files:
        if not data_file.filename:
            continue
        suffix = Path(data_file.filename).suffix.lower()
        if suffix not in _ALLOWED_DATA_FILE_EXTENSIONS:
            allowed = "/".join(sorted(_ALLOWED_DATA_FILE_EXTENSIONS))
            raise HTTPException(
                status_code=400,
                detail=f"{data_file.filename!r}: 不支持的文件类型，只支持 {allowed}",
            )
```

在 `start_schema_etl_run` 函数里，找到第 196-207 行这一段（已知，供比对）：

```python
    if parsed_config.tenant_id != tenant_id:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail=f"配置文件里的 tenant_id {parsed_config.tenant_id!r} 与当前操作的租户 {tenant_id!r} 不一致",
        )

    for data_file in data_files:
        if not data_file.filename:
            continue
        dest = run_dir / _sanitize_data_filename(data_file.filename)
        dest.write_bytes(await data_file.read())
```

改成（在 tenant_id 一致性校验和文件写盘循环之间插入白名单校验）：

```python
    if parsed_config.tenant_id != tenant_id:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail=f"配置文件里的 tenant_id {parsed_config.tenant_id!r} 与当前操作的租户 {tenant_id!r} 不一致",
        )

    try:
        _validate_data_file_extensions(data_files)
    except HTTPException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise

    for data_file in data_files:
        if not data_file.filename:
            continue
        dest = run_dir / _sanitize_data_filename(data_file.filename)
        dest.write_bytes(await data_file.read())
```

- [ ] **Step 4: 跑测试确认全部通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_admin_schema_etl_routes.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/admin_schema_etl_routes.py tests/api/test_admin_schema_etl_routes.py
git commit -m "feat(schema-etl): reject unsupported data file extensions at upload time"
```

---

### Task 6: 前端表头读取器通用化 + UI 入口更新

**Files:**
- Create: `frontend/src/admin/schemaEtlConfigBuilder/tableHeader.ts`
- Delete: `frontend/src/admin/schemaEtlConfigBuilder/csvHeader.ts`
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/SchemaEtlConfigBuilder.tsx`
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`
- Modify: `frontend/package.json`

**Interfaces:**
- Consumes：无
- Produces：`readTableHeaderColumns(file: File): Promise<string[]>`（替代原来的 `readCsvHeaderColumns`，两个调用方都要改名）

- [ ] **Step 1: 新增前端依赖**

修改 `frontend/package.json` 的 `dependencies` 块。当前内容（已知，供比对）：

```json
  "dependencies": {
    "@fontsource/space-grotesk": "^5.3.0",
    "@fontsource/space-mono": "^5.3.0",
    "katex": "^0.18.4",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-markdown": "^10.1.0",
    "react-router-dom": "^7.18.2",
    "rehype-katex": "^7.0.1",
    "remark-gfm": "^4.0.1",
    "remark-math": "^6.0.0"
  },
```

改成（按字母顺序插入 `xlsx`）：

```json
  "dependencies": {
    "@fontsource/space-grotesk": "^5.3.0",
    "@fontsource/space-mono": "^5.3.0",
    "katex": "^0.18.4",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-markdown": "^10.1.0",
    "react-router-dom": "^7.18.2",
    "rehype-katex": "^7.0.1",
    "remark-gfm": "^4.0.1",
    "remark-math": "^6.0.0",
    "xlsx": "^0.18.5"
  },
```

Run: `cd frontend && npm install`
Expected: 安装成功，`node_modules/xlsx` 存在，`package-lock.json` 更新

- [ ] **Step 2: 读取当前 `csvHeader.ts` 完整内容**

当前内容（已知，供比对，稍后要整体搬到新文件并扩展）：

```ts
// 只读文件开头一小段就够了——表头只在第一行，不需要把整个文件读进内存。
// 64KB 远超任何现实场景下单行表头的长度（哪怕几百个中文列名也远远不到这个量级）。
const HEADER_READ_BYTES = 65536

export async function readCsvHeaderColumns(file: File): Promise<string[]> {
  const chunk = await file.slice(0, HEADER_READ_BYTES).text()
  const firstLineEnd = chunk.search(/\r\n|\r|\n/)
  const firstLine = firstLineEnd === -1 ? chunk : chunk.slice(0, firstLineEnd)
  return parseCsvHeaderLine(firstLine)
}

// 按标准 CSV 引号规则（RFC 4180）解析一行，跟后端 Python csv 模块的解析规则
// 对齐——如果表头列名里本身带逗号，必须用双引号包裹（如 "A,B"），双引号
// 内部的字面双引号写成两个连续双引号（""）转义，这里同样处理这两种情况。
function parseCsvHeaderLine(line: string): string[] {
  const columns: string[] = []
  let current = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const char = line[i]
    if (inQuotes) {
      if (char === '"') {
        if (line[i + 1] === '"') {
          current += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        current += char
      }
    } else if (char === '"') {
      inQuotes = true
    } else if (char === ',') {
      columns.push(current)
      current = ''
    } else {
      current += char
    }
  }
  columns.push(current)
  return columns.map((c) => c.trim())
}
```

- [ ] **Step 3: 新建 `frontend/src/admin/schemaEtlConfigBuilder/tableHeader.ts`**

完整内容：

```ts
import * as XLSX from 'xlsx'

// 只读文件开头一小段就够了——表头只在第一行，不需要把整个文件读进内存。
// 64KB 远超任何现实场景下单行表头的长度（哪怕几百个中文列名也远远不到这个量级）。
const HEADER_READ_BYTES = 65536

// 按扩展名分流：CSV/TSV 是纯文本，只读文件开头一小段当文本解析；XLSX/XLS
// 是二进制容器格式，slice().text() 这种读法完全不适用，必须交给 SheetJS
// 解析（用 sheetRows: 1 限制只解析表头所在的第一行，同样不需要把整个
// 工作簿读进内存）。固定读第一个工作表，其余 sheet 忽略——见
// docs/superpowers/specs/2026-08-21-schema-etl-multi-format-upload.md 决策 2。
export async function readTableHeaderColumns(file: File): Promise<string[]> {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (extension === '.xlsx' || extension === '.xls') {
    return readExcelHeaderColumns(file)
  }
  const delimiter = extension === '.tsv' ? '\t' : ','
  return readDelimitedHeaderColumns(file, delimiter)
}

async function readDelimitedHeaderColumns(file: File, delimiter: string): Promise<string[]> {
  const chunk = await file.slice(0, HEADER_READ_BYTES).text()
  const firstLineEnd = chunk.search(/\r\n|\r|\n/)
  const firstLine = firstLineEnd === -1 ? chunk : chunk.slice(0, firstLineEnd)
  return parseDelimitedHeaderLine(firstLine, delimiter)
}

// 按标准 CSV 引号规则（RFC 4180）解析一行，跟后端 Python csv 模块的解析规则
// 对齐——如果表头列名里本身带分隔符，必须用双引号包裹（如 "A,B"），双引号
// 内部的字面双引号写成两个连续双引号（""）转义，这里同样处理这两种情况。
// TSV 复用同一套引号规则，只是把逗号换成传入的 delimiter。
function parseDelimitedHeaderLine(line: string, delimiter: string): string[] {
  const columns: string[] = []
  let current = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const char = line[i]
    if (inQuotes) {
      if (char === '"') {
        if (line[i + 1] === '"') {
          current += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        current += char
      }
    } else if (char === '"') {
      inQuotes = true
    } else if (char === delimiter) {
      columns.push(current)
      current = ''
    } else {
      current += char
    }
  }
  columns.push(current)
  return columns.map((c) => c.trim())
}

async function readExcelHeaderColumns(file: File): Promise<string[]> {
  const buffer = await file.arrayBuffer()
  const workbook = XLSX.read(buffer, { type: 'array', sheetRows: 1 })
  const firstSheetName = workbook.SheetNames[0]
  if (!firstSheetName) return []
  const sheet = workbook.Sheets[firstSheetName]
  const rows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1 })
  const headerRow = rows[0] ?? []
  return headerRow.map((cell) => (cell === undefined || cell === null ? '' : String(cell).trim()))
}
```

- [ ] **Step 4: 删除旧文件**

```bash
rm frontend/src/admin/schemaEtlConfigBuilder/csvHeader.ts
```

- [ ] **Step 5: 更新 `SchemaEtlConfigBuilder.tsx` 的 import 和调用**

第 5 行（已知，供比对）：

```ts
import { readCsvHeaderColumns } from './csvHeader'
```

改成：

```ts
import { readTableHeaderColumns } from './tableHeader'
```

第 74 行（已知，供比对）：

```ts
        const columns = await readCsvHeaderColumns(file)
```

改成：

```ts
        const columns = await readTableHeaderColumns(file)
```

第 108-110 行（已知，供比对）：

```tsx
        <input
          type="file"
          accept=".csv"
```

改成：

```tsx
        <input
          type="file"
          accept=".csv,.tsv,.xlsx,.xls"
```

- [ ] **Step 6: 更新 `SchemaEtlPage.tsx` 的 `accept` 属性和说明文字**

第 437-442 行（已知，供比对）：

```tsx
              <label className="flex flex-col gap-1 text-sm font-bold text-ink">
                数据文件（CSV，可多选）
                <input
                  type="file"
                  name="data_files"
                  accept=".csv"
```

改成：

```tsx
              <label className="flex flex-col gap-1 text-sm font-bold text-ink">
                数据文件（CSV/TSV/XLSX/XLS，可多选）
                <input
                  type="file"
                  name="data_files"
                  accept=".csv,.tsv,.xlsx,.xls"
```

- [ ] **Step 7: 全文件检索确认没有其它地方还在引用旧的 `csvHeader`/`readCsvHeaderColumns`**

Run: `cd frontend && grep -rn "csvHeader\|readCsvHeaderColumns" src/`
Expected: 无输出（全部改完了）

- [ ] **Step 8: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 9: Commit**

```bash
git add frontend/package.json frontend/package-lock.json \
  frontend/src/admin/schemaEtlConfigBuilder/tableHeader.ts \
  frontend/src/admin/schemaEtlConfigBuilder/SchemaEtlConfigBuilder.tsx \
  frontend/src/admin/SchemaEtlPage.tsx
git rm frontend/src/admin/schemaEtlConfigBuilder/csvHeader.ts
git commit -m "feat(schema-etl): support XLSX/XLS/TSV header reading in upload wizard"
```

---

## 手工验证（全部任务完成后，浏览器里逐项确认）

本 session 没有浏览器自动化工具，以下写成"预期结果描述"，由人工或后续有浏览器工具的场景执行：

1. 打开后台「数据加工」→「表格导入」tab，向导的"添加数据文件"步骤，文件选择器应该能选中 `.csv`/`.tsv`/`.xlsx`/`.xls` 四种后缀的文件（之前只能选 `.csv`）。
2. 上传一个真实的 `.xlsx` 文件（比如用 Excel/WPS 随手做一张两列的表，第一行是表头），预期能在"添加数据文件"步骤的文件列表里看到正确的列数，后续"配置实体映射"步骤的列名下拉框里能看到跟表头一致的列名。
3. 用同一份数据分别做成 `.csv` 和 `.xlsx` 两个文件，各自配置好实体映射后提交跑一次 ETL，预期两次跑出来的 `report.csv`（写入/跳过统计）完全一致——验证 XLSX 路径跟 CSV 路径产出等价结果。
4. 上传一个数据文件后缀是 `.pdf`（或任何不在白名单里的类型），提交时预期后端返回 400，错误信息里能看到具体是哪个文件名不支持，前端"确认并开始运行"按钮下方能看到这条错误提示（`SchemaEtlConfigBuilder.tsx` 已有的 `submitError` 展示逻辑，不需要额外改动）。
5. 在 Excel 里另存一份用 GBK 编码保存的 `.csv`（Windows 记事本"另存为"选 ANSI 编码在国内系统下通常就是 GBK），上传后预期能正常解析出中文列名和值，不报解码错误。
