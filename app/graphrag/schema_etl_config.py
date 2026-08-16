from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class InvalidSchemaETLConfigError(Exception):
    """列映射配置格式不合法——缺 tenant_id、entity 没有 node_key_parts 等。"""


@dataclass(frozen=True)
class ColumnNodeKeyPart:
    column: str


@dataclass(frozen=True)
class AllocatedCodeNodeKeyPart:
    scope_columns: list[str]
    raw_value_column: str


@dataclass(frozen=True)
class EntityMapping:
    term_type: str
    source_file: str
    product_line: str
    standard_name_column: str
    node_key_parts: list[ColumnNodeKeyPart | AllocatedCodeNodeKeyPart]
    field_mappings: dict[str, str]


@dataclass(frozen=True)
class RelationMapping:
    relation_type: str
    source_file: str
    subject_term_type: str
    object_term_type: str


@dataclass(frozen=True)
class SchemaETLConfig:
    tenant_id: str
    entities: list[EntityMapping]
    relations: list[RelationMapping]


def _parse_node_key_part(raw: dict) -> ColumnNodeKeyPart | AllocatedCodeNodeKeyPart:
    if "column" in raw:
        return ColumnNodeKeyPart(column=raw["column"])
    if "allocated_code" in raw:
        allocated = raw["allocated_code"]
        try:
            return AllocatedCodeNodeKeyPart(
                scope_columns=list(allocated["scope_columns"]),
                raw_value_column=allocated["raw_value_column"],
            )
        except KeyError as e:
            raise InvalidSchemaETLConfigError(
                f"allocated_code 缺少必需字段 {e.args[0]!r}: {allocated!r}"
            ) from e
    raise InvalidSchemaETLConfigError(
        f"node_key_parts 元素必须是 {{'column': ...}} 或 {{'allocated_code': ...}}，收到: {raw!r}"
    )


def _parse_entity_mapping(raw: dict) -> EntityMapping:
    try:
        node_key_parts_raw = raw.get("node_key_parts") or []
        if not node_key_parts_raw:
            raise InvalidSchemaETLConfigError(
                f"实体类型 {raw.get('term_type')!r} 的 node_key_parts 不能为空"
            )
        return EntityMapping(
            term_type=raw["term_type"],
            source_file=raw["source_file"],
            product_line=raw["product_line"],
            standard_name_column=raw["standard_name_column"],
            node_key_parts=[_parse_node_key_part(part) for part in node_key_parts_raw],
            field_mappings=dict(raw.get("field_mappings") or {}),
        )
    except KeyError as e:
        raise InvalidSchemaETLConfigError(
            f"实体映射缺少必需字段 {e.args[0]!r}: {raw!r}"
        ) from e


def _parse_relation_mapping(raw: dict) -> RelationMapping:
    try:
        return RelationMapping(
            relation_type=raw["relation_type"],
            source_file=raw["source_file"],
            subject_term_type=raw["subject_term_type"],
            object_term_type=raw["object_term_type"],
        )
    except KeyError as e:
        raise InvalidSchemaETLConfigError(
            f"关系映射缺少必需字段 {e.args[0]!r}: {raw!r}"
        ) from e


def load_schema_etl_config(path: Path) -> SchemaETLConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "tenant_id" not in data:
        raise InvalidSchemaETLConfigError(f"配置文件缺少 tenant_id: {path}")
    return SchemaETLConfig(
        tenant_id=data["tenant_id"],
        entities=[_parse_entity_mapping(raw) for raw in data.get("entities") or []],
        relations=[_parse_relation_mapping(raw) for raw in data.get("relations") or []],
    )
