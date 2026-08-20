# ETL 示例数据生成器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在"结构化数据加工"（Schema ETL）页面新增一个"生成示例数据"能力——基于租户已确认的本体 schema，自动生成一套可以直接原样跑通的 `config.yaml` + 配套 CSV 示例文件，页面内可预览、可打包下载，帮助用户理解列映射配置该怎么写。

**Architecture:** 新增一个纯函数生成核心（`app/graphrag/schema_etl_sample.py`），输入已确认的 `term_types`/`allowed_combinations`（调用方负责查库），输出有序的 `SampleFile` 列表；`app/api/admin_schema_etl_routes.py` 新增两个只读 GET 端点复用同一个生成核心，一个返回 JSON 供页面预览、一个把同样的内容打包成 zip 供下载；`frontend/src/admin/SchemaEtlPage.tsx` 新增一个默认折叠的"查看示例数据"区块，展开时拉取预览接口，渲染"文件列表 + 选中文件内容"，底部提供 zip 下载按钮。全程不写任何数据库表，不占用"跑批历史"。

**Tech Stack:** Python 3.12 / FastAPI / aiosqlite / PyYAML（后端）；React + TypeScript（前端），沿用页面已有的 `adminFetch`/blob 下载模式，不引入新依赖。

**Spec:** `docs/superpowers/specs/2026-08-20-schema-etl-sample-generator-design.md`

## Global Constraints

- 生成核心是纯函数，不接收数据库连接——调用方（路由层）负责先查库、再把结果列表传进来，方便单元测试直接喂造好的 `TermTypeCategory`/`AllowedCombination` 列表，不需要整套 sqlite fixture。
- 不写任何数据库表，不引入 `run_id`/状态机，不占用 `etl_runs_store` 的"历史跑批"列表。
- 每个已确认的 `(subject_term_type, relation_type, object_term_type)` 组合各生成一份独立的关系文件，不按 `relation_type` 去重合并。
- 生成的 `node_key_parts` 只用简单写法 `{column: ...}`，不生成 `allocated_code` 进阶写法。
- 每个 CSV 文件生成 2 行示例数据。
- `field_mappings` 里 CSV 源列名故意与本体声明的字段名不同（`{field_name}列`），不能让两者长得一样。
- `config.yaml` 的 `relations` 段没有任何关系时显式写成空列表 `[]`，不省略这个 key。
- 两个新路由都先检查 `is_ontology_confirmed`（`False` → `400`）；生成核心内部遇到零个已确认实体类型时抛 `EmptySchemaError`，路由层捕获后转 `400`。
- 生成的文件名（`{term_type}.csv`、`{subject}_{relation_type}_{object}.csv`）在拼接前必须对 `term_type`/`subject_term_type`/`object_term_type` 做路径安全消毒（防止 Zip Slip：这些值是租户可自由输入的字符串，没有字符集限制，原样拼进 zip 条目名可能在用户本地解压时逃出目标目录）。
- 前端预览用 `<pre>` 纯文本展示，不做语法高亮；下载走页面已有的 blob → 临时 `<a>` 下载模式（照抄 `handleDownloadReport` 的写法），不新增第三方 zip/下载库。

---

### Task 1: 生成核心 `app/graphrag/schema_etl_sample.py`

**Files:**
- Create: `app/graphrag/schema_etl_sample.py`
- Test: `tests/graphrag/test_schema_etl_sample.py`

**Interfaces:**
- Consumes：`app.graphrag.ontology_categories.TermTypeCategory`（字段：`value: str`, `extra_fields: list[ExtraFieldSpec]`）、`app.graphrag.ontology_categories.ExtraFieldSpec`（字段：`name: str`, `value_type: str`，取值 `"string"`/`"number"`/`"integer"`/`"number[]"`）、`app.graphrag.ontology_constraints.AllowedCombination`（字段：`subject_term_type: str`, `relation_type: str`, `object_term_type: str`）。这三个 dataclass 均已存在，本任务不修改它们。
- Produces：
  - `class EmptySchemaError(Exception)` —— 零个已确认实体类型时抛出。
  - `@dataclass(frozen=True) class SampleFile` —— 字段 `filename: str`, `content: str`。
  - `def generate_schema_etl_sample_files(*, tenant_id: str, term_types: list[TermTypeCategory], allowed_combinations: list[AllowedCombination]) -> list[SampleFile]` —— 同步纯函数，Task 2 的路由层会调用它。返回列表第 0 项固定是 `SampleFile(filename="config.yaml", ...)`。

- [ ] **Step 1: 写生成核心失败的测试（空 schema）**

创建 `tests/graphrag/test_schema_etl_sample.py`：

```python
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
import yaml

from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination
from app.graphrag.schema_etl_config import load_schema_etl_config
from app.graphrag.schema_etl_sample import (
    EmptySchemaError,
    SampleFile,
    generate_schema_etl_sample_files,
)

pytestmark = pytest.mark.anyio


def _csv_rows(content: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content)))


def test_generate_raises_when_no_confirmed_term_types():
    with pytest.raises(EmptySchemaError):
        generate_schema_etl_sample_files(tenant_id="demo", term_types=[], allowed_combinations=[])
```

- [ ] **Step 2: 运行测试，确认失败（函数还不存在）**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.graphrag.schema_etl_sample'`

- [ ] **Step 3: 写生成核心的最小实现（空 schema 分支）**

创建 `app/graphrag/schema_etl_sample.py`：

```python
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

import yaml

from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination


class EmptySchemaError(Exception):
    """租户没有任何已确认的实体类型，没有可以拿来生成示例的本体依据。"""


@dataclass(frozen=True)
class SampleFile:
    filename: str
    content: str


# term_type/relation_type 都是租户可自由输入的字符串（relation_type 虽然有
# ^[A-Z][A-Z0-9_]{0,63}$ 的格式校验，term_type 完全没有字符集限制），原样
# 拼进生成的文件名会有 Zip Slip 风险——用户本地解压这份 zip 时，一个形如
# "../../evil" 的 term_type 值可能逃出目标目录。这里只消毒路径分隔符等
# 危险字符，不追求生成"好看"的文件名。
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)


def _sanitize_filename_component(name: str) -> str:
    sanitized = _UNSAFE_NAME_CHARS.sub("_", name) or "unnamed"
    if sanitized in (".", ".."):
        sanitized = f"_{sanitized}"
    return sanitized


def generate_schema_etl_sample_files(
    *,
    tenant_id: str,
    term_types: list[TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
) -> list[SampleFile]:
    if not term_types:
        raise EmptySchemaError(f"租户 {tenant_id!r} 没有任何已确认的实体类型，无法生成示例")
    raise NotImplementedError
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: PASS（`test_generate_raises_when_no_confirmed_term_types` 通过）

- [ ] **Step 5: 写实体 CSV 生成的测试（有属性字段 + 无属性字段两种情况）**

追加到 `tests/graphrag/test_schema_etl_sample.py`：

```python
def test_entity_csv_includes_node_key_name_and_field_columns_with_two_example_rows():
    term_types = [
        TermTypeCategory(
            value="商品",
            extra_fields=[
                ExtraFieldSpec(name="价格", value_type="number"),
                ExtraFieldSpec(name="型号", value_type="string"),
            ],
        ),
    ]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    entity_file = next(f for f in files if f.filename == "商品.csv")
    rows = _csv_rows(entity_file.content)
    assert len(rows) == 2
    assert rows[0] == {
        "商品编号": "商品001",
        "商品名称": "示例商品1",
        "价格列": "1.5",
        "型号列": "示例文本1",
    }
    assert rows[1] == {
        "商品编号": "商品002",
        "商品名称": "示例商品2",
        "价格列": "2.5",
        "型号列": "示例文本2",
    }


def test_entity_csv_with_no_extra_fields_only_has_node_key_and_name_columns():
    term_types = [TermTypeCategory(value="品类", extra_fields=[])]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    entity_file = next(f for f in files if f.filename == "品类.csv")
    rows = _csv_rows(entity_file.content)
    assert rows == [
        {"品类编号": "品类001", "品类名称": "示例品类1"},
        {"品类编号": "品类002", "品类名称": "示例品类2"},
    ]


def test_integer_and_number_array_value_types_generate_expected_example_values():
    term_types = [
        TermTypeCategory(
            value="库存",
            extra_fields=[
                ExtraFieldSpec(name="数量", value_type="integer"),
                ExtraFieldSpec(name="坐标", value_type="number[]"),
            ],
        ),
    ]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    rows = _csv_rows(next(f for f in files if f.filename == "库存.csv").content)
    assert rows[0]["数量列"] == "1"
    assert rows[1]["数量列"] == "2"
    assert rows[0]["坐标列"] == "1.5;2.5"
    assert rows[1]["坐标列"] == "3.5;4.5"
```

- [ ] **Step 6: 运行测试，确认失败**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: FAIL（`NotImplementedError`，或者 `next()` 找不到匹配文件而 `StopIteration`）

- [ ] **Step 7: 实现实体 CSV 生成逻辑**

在 `app/graphrag/schema_etl_sample.py` 里，`generate_schema_etl_sample_files` 的
`raise NotImplementedError` 之前插入以下辅助函数和逻辑（替换掉整个函数体）：

```python
_STRING_EXAMPLE_VALUES = ("示例文本1", "示例文本2")
_NUMBER_EXAMPLE_VALUES = ("1.5", "2.5")
_INTEGER_EXAMPLE_VALUES = ("1", "2")
_NUMBER_ARRAY_EXAMPLE_VALUES = ("1.5;2.5", "3.5;4.5")


def _example_values_for(value_type: str) -> tuple[str, str]:
    """跟 app/graphrag/schema_etl_row_processing.py::convert_field_value 的
    转换规则对齐（number[] 用分号分隔，对应 raw_value.split(";")）。"""
    if value_type == "string":
        return _STRING_EXAMPLE_VALUES
    if value_type == "number":
        return _NUMBER_EXAMPLE_VALUES
    if value_type == "integer":
        return _INTEGER_EXAMPLE_VALUES
    if value_type == "number[]":
        return _NUMBER_ARRAY_EXAMPLE_VALUES
    raise ValueError(f"未知的 value_type: {value_type!r}")


def _node_key_column(term_type: str) -> str:
    return f"{term_type}编号"


def _standard_name_column(term_type: str) -> str:
    return f"{term_type}名称"


def _node_key_example(term_type: str, row_index: int) -> str:
    return f"{term_type}{row_index:03d}"


def _standard_name_example(term_type: str, row_index: int) -> str:
    return f"示例{term_type}{row_index}"


def _field_source_column(field_name: str) -> str:
    return f"{field_name}列"


def _write_csv(fieldnames: list[str], rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _entity_sample_file(term_type: TermTypeCategory) -> SampleFile:
    node_key_col = _node_key_column(term_type.value)
    name_col = _standard_name_column(term_type.value)
    fieldnames = [node_key_col, name_col] + [
        _field_source_column(f.name) for f in term_type.extra_fields
    ]
    rows: list[dict[str, str]] = []
    for i in (1, 2):
        row = {
            node_key_col: _node_key_example(term_type.value, i),
            name_col: _standard_name_example(term_type.value, i),
        }
        for field in term_type.extra_fields:
            row[_field_source_column(field.name)] = _example_values_for(field.value_type)[i - 1]
        rows.append(row)
    filename = f"{_sanitize_filename_component(term_type.value)}.csv"
    return SampleFile(filename=filename, content=_write_csv(fieldnames, rows))
```

然后把 `generate_schema_etl_sample_files` 函数体替换成：

```python
def generate_schema_etl_sample_files(
    *,
    tenant_id: str,
    term_types: list[TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
) -> list[SampleFile]:
    if not term_types:
        raise EmptySchemaError(f"租户 {tenant_id!r} 没有任何已确认的实体类型，无法生成示例")
    files = [SampleFile(filename="config.yaml", content="")]  # 占位，Step 11 补上真正内容
    files.extend(_entity_sample_file(t) for t in term_types)
    return files
```

- [ ] **Step 8: 运行测试，确认三个新测试通过**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: PASS（Step 5 的三个测试通过；Step 1 的空 schema 测试仍然通过）

- [ ] **Step 9: 写关系 CSV 生成的测试（不同类型 + 相同类型自关联两种情况）**

追加到 `tests/graphrag/test_schema_etl_sample.py`：

```python
def test_relation_csv_includes_subject_and_object_node_key_columns():
    term_types = [
        TermTypeCategory(value="商品", extra_fields=[]),
        TermTypeCategory(value="品类", extra_fields=[]),
    ]
    combos = [AllowedCombination(subject_term_type="商品", relation_type="BELONG_TO", object_term_type="品类")]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )

    relation_file = next(f for f in files if f.filename == "商品_BELONG_TO_品类.csv")
    rows = _csv_rows(relation_file.content)
    assert rows == [
        {"商品编号": "商品001", "品类编号": "品类001"},
        {"商品编号": "商品002", "品类编号": "品类002"},
    ]


def test_relation_csv_for_self_relation_reuses_one_shared_column():
    term_types = [TermTypeCategory(value="品类", extra_fields=[])]
    combos = [AllowedCombination(subject_term_type="品类", relation_type="RELATED_TO", object_term_type="品类")]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )

    relation_file = next(f for f in files if f.filename == "品类_RELATED_TO_品类.csv")
    rows = _csv_rows(relation_file.content)
    # 主体/客体是同一个 term_type 时，node_key 列名相同，配置格式本身没法用
    # 一行 CSV 表达"品类 A 关联到不同的品类 B"——这不是本生成器的缺陷，是
    # schema_etl_row_processing.py::compute_node_key 对主体/客体各自独立取
    # 同名列的既有行为（两次取值天然相同）。示例如实反映这个真实限制。
    assert rows == [{"品类编号": "品类001"}, {"品类编号": "品类002"}]
```

- [ ] **Step 10: 运行测试，确认失败**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: FAIL（`StopIteration`，还没有生成关系文件）

- [ ] **Step 11: 实现关系 CSV 生成逻辑，并补上真正的 config.yaml 内容**

在 `_entity_sample_file` 函数之后追加：

```python
def _relation_sample_file(combo: AllowedCombination) -> SampleFile:
    subject_col = _node_key_column(combo.subject_term_type)
    object_col = _node_key_column(combo.object_term_type)
    same_column = subject_col == object_col
    fieldnames = [subject_col] if same_column else [subject_col, object_col]
    rows: list[dict[str, str]] = []
    for i in (1, 2):
        row = {subject_col: _node_key_example(combo.subject_term_type, i)}
        if not same_column:
            row[object_col] = _node_key_example(combo.object_term_type, i)
        rows.append(row)
    filename = (
        f"{_sanitize_filename_component(combo.subject_term_type)}_"
        f"{combo.relation_type}_"
        f"{_sanitize_filename_component(combo.object_term_type)}.csv"
    )
    return SampleFile(filename=filename, content=_write_csv(fieldnames, rows))


def _config_yaml(
    *,
    tenant_id: str,
    term_types: list[TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
) -> str:
    entities = [
        {
            "term_type": t.value,
            "source_file": f"{_sanitize_filename_component(t.value)}.csv",
            "standard_name_column": _standard_name_column(t.value),
            "node_key_parts": [{"column": _node_key_column(t.value)}],
            "field_mappings": {f.name: _field_source_column(f.name) for f in t.extra_fields},
        }
        for t in term_types
    ]
    relations = [
        {
            "relation_type": c.relation_type,
            "source_file": (
                f"{_sanitize_filename_component(c.subject_term_type)}_"
                f"{c.relation_type}_"
                f"{_sanitize_filename_component(c.object_term_type)}.csv"
            ),
            "subject_term_type": c.subject_term_type,
            "object_term_type": c.object_term_type,
        }
        for c in allowed_combinations
    ]
    data = {"tenant_id": tenant_id, "entities": entities, "relations": relations}
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
```

再把 `generate_schema_etl_sample_files` 替换成最终版本：

```python
def generate_schema_etl_sample_files(
    *,
    tenant_id: str,
    term_types: list[TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
) -> list[SampleFile]:
    if not term_types:
        raise EmptySchemaError(f"租户 {tenant_id!r} 没有任何已确认的实体类型，无法生成示例")
    files = [
        SampleFile(
            filename="config.yaml",
            content=_config_yaml(
                tenant_id=tenant_id,
                term_types=term_types,
                allowed_combinations=allowed_combinations,
            ),
        )
    ]
    files.extend(_entity_sample_file(t) for t in term_types)
    files.extend(_relation_sample_file(c) for c in allowed_combinations)
    return files
```

- [ ] **Step 12: 运行测试，确认全部通过**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: PASS（全部 6 个测试通过）

- [ ] **Step 13: 写 config.yaml 往返解析测试 + 文件名消毒测试**

追加到 `tests/graphrag/test_schema_etl_sample.py`：

```python
def test_config_yaml_round_trips_through_load_schema_etl_config(tmp_path):
    term_types = [
        TermTypeCategory(
            value="商品",
            extra_fields=[ExtraFieldSpec(name="价格", value_type="number")],
        ),
        TermTypeCategory(value="品类", extra_fields=[]),
    ]
    combos = [AllowedCombination(subject_term_type="商品", relation_type="BELONG_TO", object_term_type="品类")]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )
    config_file = next(f for f in files if f.filename == "config.yaml")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_file.content, encoding="utf-8")

    config = load_schema_etl_config(config_path)

    assert config.tenant_id == "demo"
    assert [e.term_type for e in config.entities] == ["商品", "品类"]
    product_mapping = config.entities[0]
    assert product_mapping.source_file == "商品.csv"
    assert product_mapping.standard_name_column == "商品名称"
    assert product_mapping.field_mappings == {"价格": "价格列"}
    assert len(config.relations) == 1
    assert config.relations[0].source_file == "商品_BELONG_TO_品类.csv"


def test_config_yaml_has_empty_relations_list_when_no_combinations_confirmed():
    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=[TermTypeCategory(value="品类", extra_fields=[])],
        allowed_combinations=[],
    )
    config_file = next(f for f in files if f.filename == "config.yaml")
    parsed = yaml.safe_load(config_file.content)
    assert parsed["relations"] == []


def test_unsafe_term_type_characters_are_sanitized_out_of_the_filename():
    term_types = [TermTypeCategory(value="a/../b", extra_fields=[])]

    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=[]
    )

    filenames = [f.filename for f in files]
    assert "config.yaml" in filenames
    entity_filenames = [f for f in filenames if f != "config.yaml"]
    assert len(entity_filenames) == 1
    assert "/" not in entity_filenames[0]
    assert ".." not in entity_filenames[0]
```

- [ ] **Step 14: 运行测试，确认全部通过**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: PASS（全部 9 个测试通过）

- [ ] **Step 15: 写端到端集成测试——生成的示例真的能被 run_schema_etl 跑通**

追加到 `tests/graphrag/test_schema_etl_sample.py`（复用 `tests/graphrag/test_schema_etl.py` 里
已有的 fixture 写法，本文件独立构造一份最小 fixture，不导入那个文件）：

```python
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.ontology_categories import create_term_type, list_term_types
from app.graphrag.ontology_constraints import add_allowed_combination, list_allowed_combinations
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema
from app.graphrag.ontology_relations import create_relation_type
from app.graphrag.schema_etl import run_schema_etl
from app.graphrag.terms_store import ensure_terms_schema


class _FakeGraphClient:
    async def sync_term(self, term) -> None:
        pass

    async def merge_relation(self, **kwargs) -> None:
        pass


async def test_generated_sample_files_run_successfully_through_run_schema_etl(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_stable_code_registry_schema(conn)
    await create_term_type(
        conn, tenant_id="demo", value="商品",
        extra_fields=[ExtraFieldSpec(name="价格", value_type="number")],
    )
    await create_term_type(conn, tenant_id="demo", value="品类")
    await checkout_draft(conn, "demo")
    await create_relation_type(
        conn, "demo", relation_type="BELONG_TO", example_phrase="商品 BELONG_TO 品类",
    )
    await add_allowed_combination(
        conn, "demo", subject_term_type="商品", relation_type="BELONG_TO", object_term_type="品类",
    )
    await confirm_ontology(conn, "demo")

    term_types = await list_term_types(conn, "demo", status="confirmed")
    combos = await list_allowed_combinations(conn, "demo", status="confirmed")
    files = generate_schema_etl_sample_files(
        tenant_id="demo", term_types=term_types, allowed_combinations=combos
    )
    for f in files:
        (tmp_path / f.filename).write_text(f.content, encoding="utf-8")
    config = load_schema_etl_config(tmp_path / "config.yaml")

    report = await run_schema_etl(
        conn=conn, graph_client=_FakeGraphClient(), config=config, data_dir=tmp_path
    )

    assert report.entities_written == 4  # 2 行商品 + 2 行品类
    assert report.entities_skipped == 0
    assert report.relations_written == 2
    assert report.relations_skipped == 0
```

- [ ] **Step 16: 运行测试，确认通过**

Run: `python -m pytest tests/graphrag/test_schema_etl_sample.py -v`
Expected: PASS（全部 10 个测试通过，含端到端集成测试）

- [ ] **Step 17: Commit**

```bash
git add app/graphrag/schema_etl_sample.py tests/graphrag/test_schema_etl_sample.py
git commit -m "feat(graphrag): add schema ETL sample data generator"
```

---

### Task 2: 新增两个 API 端点

**Files:**
- Modify: `app/api/admin_schema_etl_routes.py`
- Test: `tests/api/test_admin_schema_etl_routes.py`

**Interfaces:**
- Consumes：Task 1 的 `app.graphrag.schema_etl_sample.{EmptySchemaError, SampleFile, generate_schema_etl_sample_files}`；已有的 `app.graphrag.ontology_categories.list_term_types`（签名 `async def list_term_types(conn, tenant_id, *, status) -> list[TermTypeCategory]`）；已有的 `app.graphrag.ontology_constraints.list_allowed_combinations`（签名 `async def list_allowed_combinations(conn, tenant_id, *, status) -> list[AllowedCombination]`）；已有的 `app.graphrag.ontology_lifecycle.is_ontology_confirmed`（已在本文件顶部导入）。
- Produces：
  - `GET /api/admin/{tenant_id}/schema-etl/sample` → `200` JSON `{"files": [{"filename": str, "content": str}, ...]}`，`files[0].filename == "config.yaml"`；`400` 当未确认或空 schema。
  - `GET /api/admin/{tenant_id}/schema-etl/sample.zip` → `200` `application/zip` 二进制流，`Content-Disposition: attachment; filename="{tenant_id}_schema_etl_sample.zip"`；`400` 语义同上。

- [ ] **Step 1: 写路由层测试（先覆盖两个 400 分支和一个成功分支）**

打开 `tests/api/test_admin_schema_etl_routes.py`。文件顶部已有
`from app.graphrag.ontology_categories import create_term_type` 和
`from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema`
——本任务新增的测试还要用到 `create_relation_type`，在文件顶部 import 区新增一行：

```python
from app.graphrag.ontology_relations import create_relation_type
```

然后在文件末尾追加（复用文件顶部已有的 `_FakeGraphClient`、`_open_review_conn` 辅助——
`_open_review_conn` 已经调用了 `ensure_ontology_schema`/`ensure_terms_schema`/
`ensure_etl_runs_schema`，本任务不需要再新增 schema 初始化）：

```python
def test_get_sample_returns_400_when_ontology_not_confirmed():
    async def override_review_conn():
        conn = await _open_review_conn()
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample")
        assert response.status_code == 400
        assert "确认" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_get_sample_returns_400_when_schema_confirmed_but_has_no_term_types():
    async def override_review_conn():
        conn = await _open_review_conn()
        await checkout_draft(conn, "demo")
        await create_relation_type(conn, "demo", relation_type="RELATED_TO", example_phrase="A RELATED_TO B")
        await confirm_ontology(conn, "demo")
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample")
        assert response.status_code == 400
        assert "没有任何已确认的实体类型" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_get_sample_returns_files_with_config_yaml_first():
    async def override_review_conn():
        conn = await _open_review_conn()
        await create_term_type(conn, tenant_id="demo", value="商品")
        await checkout_draft(conn, "demo")
        await create_relation_type(conn, "demo", relation_type="RELATED_TO", example_phrase="A RELATED_TO B")
        await confirm_ontology(conn, "demo")
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample")
        assert response.status_code == 200
        data = response.json()
        assert data["files"][0]["filename"] == "config.yaml"
        assert any(f["filename"] == "商品.csv" for f in data["files"])
    finally:
        app.dependency_overrides.clear()


def test_download_sample_zip_returns_a_valid_zip_containing_the_same_files():
    import zipfile
    import io

    async def override_review_conn():
        conn = await _open_review_conn()
        await create_term_type(conn, tenant_id="demo", value="商品")
        await checkout_draft(conn, "demo")
        await create_relation_type(conn, "demo", relation_type="RELATED_TO", example_phrase="A RELATED_TO B")
        await confirm_ontology(conn, "demo")
        yield conn
        await conn.close()

    app.dependency_overrides[deps.get_review_conn] = override_review_conn
    app.dependency_overrides[deps.require_admin_session] = lambda: None
    app.dependency_overrides[deps.get_graph_client] = lambda: _FakeGraphClient()
    client = TestClient(app)
    try:
        response = client.get("/api/admin/demo/schema-etl/sample.zip")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert "config.yaml" in archive.namelist()
        assert "商品.csv" in archive.namelist()
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: 运行测试，确认失败（路由还不存在）**

Run: `python -m pytest tests/api/test_admin_schema_etl_routes.py -v -k "sample"`
Expected: FAIL，`404 Not Found`（`assert response.status_code == 400` 之类的断言失败）

- [ ] **Step 3: 在 `admin_schema_etl_routes.py` 顶部新增 import**

在文件现有的 import 区（`import csv` 那一段）里加一行：

```python
import zipfile
```

在现有的 graphrag 相关 import 区（`from app.graphrag.etl_runs_store import (...)` 附近）加：

```python
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations
from app.graphrag.schema_etl_sample import (
    EmptySchemaError,
    SampleFile,
    generate_schema_etl_sample_files,
)
```

- [ ] **Step 4: 新增响应模型 + 共享的"取已确认样例文件"辅助函数**

在文件里 `RunDetailResponse` 类定义之后（`download_schema_etl_report_csv` 函数之前的任意
位置均可，建议紧跟在响应模型区块）追加：

```python
class SampleFileResponse(BaseModel):
    filename: str
    content: str


class SampleResponse(BaseModel):
    files: list[SampleFileResponse]


async def _build_sample_files(
    tenant_id: str, review_conn: aiosqlite.Connection
) -> list[SampleFile]:
    if not await is_ontology_confirmed(review_conn, tenant_id):
        raise HTTPException(
            status_code=400, detail=f"租户 {tenant_id!r} 的本体 schema 还没有确认"
        )
    term_types = await list_term_types(review_conn, tenant_id, status="confirmed")
    allowed_combinations = await list_allowed_combinations(review_conn, tenant_id, status="confirmed")
    try:
        return generate_schema_etl_sample_files(
            tenant_id=tenant_id, term_types=term_types, allowed_combinations=allowed_combinations,
        )
    except EmptySchemaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

- [ ] **Step 5: 新增两个路由，放在文件末尾（`download_schema_etl_report_csv` 之后）**

```python
@router.get("/sample", response_model=SampleResponse)
async def get_schema_etl_sample(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> SampleResponse:
    files = await _build_sample_files(tenant_id, review_conn)
    return SampleResponse(files=[SampleFileResponse(filename=f.filename, content=f.content) for f in files])


@router.get("/sample.zip")
async def download_schema_etl_sample_zip(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> StreamingResponse:
    files = await _build_sample_files(tenant_id, review_conn)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            zf.writestr(file.filename, file.content)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{tenant_id}_schema_etl_sample.zip"'},
    )
```

- [ ] **Step 6: 运行测试，确认全部通过**

Run: `python -m pytest tests/api/test_admin_schema_etl_routes.py -v`
Expected: PASS（新增的 4 个测试通过，且文件里原有的测试不受影响）

- [ ] **Step 7: 跑一次全量后端回归**

Run: `python -m pytest tests/ -q`
Expected: 全部通过（除已知与本次改动无关的 TTS 相关预先失败用例，如果遇到直接对照
`git stash` 前的基线确认是否为既有失败，不属于本任务范围）

- [ ] **Step 8: Commit**

```bash
git add app/api/admin_schema_etl_routes.py tests/api/test_admin_schema_etl_routes.py
git commit -m "feat(api): expose schema ETL sample preview and zip download endpoints"
```

---

### Task 3: 前端"查看示例数据"区块

**Files:**
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`

**Interfaces:**
- Consumes：Task 2 的 `GET /api/admin/{tenant_id}/schema-etl/sample`（返回 `{files: [{filename, content}]}`）、`GET /api/admin/{tenant_id}/schema-etl/sample.zip`（返回二进制 zip）。本文件已有的 `adminFetch`、`extractErrorDetail`（从 `./adminApi` 导入）、`useAdminAuth`/`useAdminTenant` 不变。
- Produces：无（叶子组件，不被其他文件消费）。

本任务没有自动化测试（该代码库前端未接入测试框架，仅用 `tsc --noEmit` 做类型级验证，
延续本 session 一贯的前端验证方式）。

- [ ] **Step 1: 新增类型定义和 state**

打开 `frontend/src/admin/SchemaEtlPage.tsx`，在现有 `interface RunDetail` 定义之后
（约第 38 行 `const SKIPPED_ROWS_PREVIEW_LIMIT = 50` 之前）插入：

```tsx
interface SampleFile {
  filename: string
  content: string
}
```

在函数体内、现有 `const [downloadingReport, setDownloadingReport] = useState(false)` 那一行
之后插入：

```tsx
  const [sampleExpanded, setSampleExpanded] = useState(false)
  const [sampleFiles, setSampleFiles] = useState<SampleFile[] | null>(null)
  const [sampleSelectedFilename, setSampleSelectedFilename] = useState<string | null>(null)
  const [sampleLoading, setSampleLoading] = useState(false)
  const [sampleError, setSampleError] = useState<string | null>(null)
  const [downloadingSample, setDownloadingSample] = useState(false)
```

- [ ] **Step 2: 新增"展开时按需加载 + 切换租户时重置缓存"的 effect**

在现有 `useEffect(() => { refreshStatus()... }, [refreshStatus])` 之后插入两个新 effect：

```tsx
  // 切换租户时，之前缓存的示例文件属于旧租户，必须清空，否则下次展开会
  // 直接复用过期数据（sampleFiles !== null 会跳过重新请求）。
  useEffect(() => {
    setSampleFiles(null)
    setSampleSelectedFilename(null)
    setSampleError(null)
  }, [tenantId])

  useEffect(() => {
    if (!sampleExpanded || sampleFiles !== null || sampleLoading || !sessionToken) return
    let cancelled = false
    const load = async () => {
      setSampleLoading(true)
      setSampleError(null)
      try {
        const response = await adminFetch(
          `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/sample`,
          sessionToken,
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '生成示例失败'))
        }
        const data = (await response.json()) as { files: SampleFile[] }
        if (cancelled) return
        setSampleFiles(data.files)
        setSampleSelectedFilename(data.files[0]?.filename ?? null)
      } catch (err) {
        if (!cancelled) setSampleError(err instanceof Error ? err.message : '生成示例失败')
      } finally {
        if (!cancelled) setSampleLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [sampleExpanded, sampleFiles, sampleLoading, sessionToken, tenantId])
```

- [ ] **Step 3: 新增下载 handler**

在现有 `handleDownloadReport` 函数之后插入：

```tsx
  const handleDownloadSample = async () => {
    if (!sessionToken || downloadingSample) return
    setSampleError(null)
    setDownloadingSample(true)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/sample.zip`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '下载示例失败'))
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${tenantId}_schema_etl_sample.zip`
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setSampleError(err instanceof Error ? err.message : '下载示例失败')
    } finally {
      setDownloadingSample(false)
    }
  }
```

- [ ] **Step 4: 新增折叠区块 JSX，插入到上传表单之前**

找到现有代码：

```tsx
      <form
        onSubmit={handleUpload}
        className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
      >
        <label className="flex flex-col gap-1 text-sm font-bold text-ink">
          列映射配置（YAML）
```

在这个 `<form>` 开始标签**之前**插入：

```tsx
      <div className="flex flex-col gap-2 border-2 border-ink bg-card shadow-brutal-sm">
        <button
          type="button"
          onClick={() => setSampleExpanded((prev) => !prev)}
          className={`flex items-center justify-between px-4 py-3 text-left font-bold text-ink ${focusRing}`}
        >
          <span>
            查看示例数据
            <span className="ml-2 font-normal text-ink-soft">
              不知道 config.yaml 和 CSV 该怎么写？点这里生成一份可以直接跑通的示例
            </span>
          </span>
          <span aria-hidden="true">{sampleExpanded ? '▾' : '▸'}</span>
        </button>
        {sampleExpanded && (
          <div className="flex flex-col gap-3 border-t-2 border-ink p-4">
            {sampleLoading && <p className="text-ink-soft">生成中…</p>}
            {sampleError && (
              <p role="alert" className="text-sm text-ink">
                {sampleError}
              </p>
            )}
            {sampleFiles && sampleFiles.length > 0 && (
              <>
                <div className="flex flex-wrap gap-2">
                  {sampleFiles.map((file) => (
                    <button
                      key={file.filename}
                      type="button"
                      onClick={() => setSampleSelectedFilename(file.filename)}
                      className={`border-2 border-ink px-3 py-1.5 text-xs font-bold text-ink shadow-brutal-sm ${
                        sampleSelectedFilename === file.filename ? 'bg-accent-pink' : 'bg-paper'
                      } ${focusRing}`}
                    >
                      {file.filename}
                    </button>
                  ))}
                </div>
                <pre className="max-h-80 overflow-auto border-2 border-ink bg-paper p-3 text-xs text-ink">
                  {sampleFiles.find((f) => f.filename === sampleSelectedFilename)?.content ?? ''}
                </pre>
                <button
                  type="button"
                  onClick={handleDownloadSample}
                  disabled={downloadingSample}
                  className={`self-start border-2 border-ink bg-paper px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {downloadingSample ? '下载中…' : '下载全部（zip）'}
                </button>
              </>
            )}
          </div>
        )}
      </div>

```

- [ ] **Step 5: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）

- [ ] **Step 6: Commit**

```bash
git add frontend/src/admin/SchemaEtlPage.tsx
git commit -m "feat(admin): add collapsible sample data preview to schema ETL page"
```

---

## 完成后的整体验证

1. `python -m pytest tests/ -q` 全绿。
2. `cd frontend && npx tsc --noEmit` 干净。
3. 手动核对（无浏览器自动化工具，口头确认设计意图）：折叠区块默认收起；展开后默认选中
   `config.yaml`；切换租户后再次展开会重新拉取而不是显示旧租户的缓存内容；未确认/空
   schema 时显示对应的错误提示而不是崩溃的文件浏览器。
