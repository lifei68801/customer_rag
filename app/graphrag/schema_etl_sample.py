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
_UNSAFE_NAME_CHARS = re.compile(r"[^\w\-]", re.UNICODE)


def _sanitize_filename_component(name: str) -> str:
    sanitized = _UNSAFE_NAME_CHARS.sub("_", name) or "unnamed"
    if sanitized in (".", ".."):
        sanitized = f"_{sanitized}"
    return sanitized


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
