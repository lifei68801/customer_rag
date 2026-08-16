from __future__ import annotations

from app.graphrag.etl_stable_code_registry import allocate_stable_code
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import AllocatedCodeNodeKeyPart, ColumnNodeKeyPart

import aiosqlite


class RowProcessingError(Exception):
    """处理某一行源数据时失败——列缺失、值转换失败、字段未声明等。写入引擎
    捕获这个异常，按 docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md
    第 6.4 节的策略跳过该行、记录日志，不中断整批。"""


async def compute_node_key(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    term_type: str,
    node_key_parts: list[ColumnNodeKeyPart | AllocatedCodeNodeKeyPart],
    row: dict[str, str],
) -> str:
    """按 node_key_parts 依次解析出各部分的值，用英文冒号拼接，前面加
    "{term_type}:" 前缀——见计划的 Global Constraints。"""
    parts: list[str] = []
    for part in node_key_parts:
        if isinstance(part, ColumnNodeKeyPart):
            if not row.get(part.column):
                raise RowProcessingError(f"node_key 需要的列 {part.column!r} 在这一行不存在或为空")
            parts.append(row[part.column])
        else:
            for scope_column in part.scope_columns:
                if not row.get(scope_column):
                    raise RowProcessingError(f"node_key 需要的作用域列 {scope_column!r} 在这一行不存在或为空")
            if not row.get(part.raw_value_column):
                raise RowProcessingError(f"node_key 需要的原始值列 {part.raw_value_column!r} 在这一行不存在或为空")
            scope = ":".join([term_type, *[row[c] for c in part.scope_columns]])
            raw_value = row[part.raw_value_column]
            allocated_code = await allocate_stable_code(
                conn, tenant_id=tenant_id, scope=scope, raw_value=raw_value
            )
            parts.append(allocated_code)
    return f"{term_type}:" + ":".join(parts)


def convert_field_value(
    *, extra_field_specs: dict[str, ExtraFieldSpec], field_name: str, raw_value: str
) -> object:
    """按已确认 schema 里该字段声明的 value_type，把 CSV 读出来的原始字符串
    转换成对应的 Python 类型——见 spec 第 4 节的转换规则表。extra_field_specs
    由调用方在处理某个 term_type 的整个源文件之前查询一次、传进来，不在这个
    函数里重复查库（避免大文件逐行查询数据库）。
    """
    if field_name not in extra_field_specs:
        raise RowProcessingError(f"字段 {field_name!r} 没有在 term_type 的 schema 里声明")
    value_type = extra_field_specs[field_name].value_type
    try:
        if value_type == "string":
            return raw_value
        if value_type == "number":
            return float(raw_value)
        if value_type == "integer":
            return int(raw_value)
        if value_type == "number[]":
            return [float(item) for item in raw_value.split(";") if item.strip()]
    except ValueError:
        raise RowProcessingError(
            f"字段 {field_name!r} 的值 {raw_value!r} 无法转换成声明的类型 {value_type!r}"
        )
    raise RowProcessingError(f"字段 {field_name!r} 声明了未知的 value_type: {value_type!r}")
