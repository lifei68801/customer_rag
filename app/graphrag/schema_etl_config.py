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
    standard_name_parts: list[str]
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
        # standard_name_column（单列）是 standard_name_parts（多列）的语法糖，
        # 归一化成单元素列表。多列写法把判别列拼进展示名，让同名不同实体在
        # 管理界面上可区分——standard_name 的唯一索引取消之后（2026-08-30），
        # 两个 William Jackson 都能落库，但展示名一样就没法人工操作了。
        parts_raw = raw.get("standard_name_parts")
        if parts_raw is None:
            column = raw.get("standard_name_column")
            if not column:
                raise InvalidSchemaETLConfigError(
                    f"实体类型 {raw.get('term_type')!r} 必须声明 "
                    f"standard_name_column（单列）或 standard_name_parts（多列）"
                )
            parts = [column]
        else:
            parts = list(parts_raw)
            if not parts:
                raise InvalidSchemaETLConfigError(
                    f"实体类型 {raw.get('term_type')!r} 的 standard_name_parts 不能为空"
                )
        return EntityMapping(
            term_type=raw["term_type"],
            source_file=raw["source_file"],
            standard_name_parts=parts,
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


def parse_schema_etl_config(text: str, *, origin: str = "<yaml>") -> SchemaETLConfig:
    """从 YAML 文本解析。origin 只用于报错里指出这段 YAML 来自哪儿。"""
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "tenant_id" not in data:
        raise InvalidSchemaETLConfigError(f"配置文件缺少 tenant_id: {origin}")
    return SchemaETLConfig(
        tenant_id=data["tenant_id"],
        entities=[_parse_entity_mapping(raw) for raw in data.get("entities") or []],
        relations=[_parse_relation_mapping(raw) for raw in data.get("relations") or []],
    )


def summarize_schema_etl_config(config: SchemaETLConfig) -> dict:
    """给界面看的映射摘要：这份映射会怎么处理一张表。

    表格导入页在「运行」按钮上方列出它——哪列当身份键、哪个属性挂在哪个实体
    下、实体之间连什么关系。此前那个表单只说"传入数据文件即可运行"，用户
    没看到过映射会做什么就点了运行；真实事故里邮编挂在了客户名下，同名客户
    邮编不同，ETL 拒绝写入——而这件事在摘要里一眼能看出来。

    每个实体给两份 node_key：

    - ``key_columns`` 是**给人看的**。稳定码分配规则（AllocatedCodeNodeKeyPart）
      对用户来说就是"按 X 列分配编号"，压成那样一句话。
    - ``key_parts`` 是**给界面回填用的**，跟 node_key_parts 一一对应、不丢信息。
      表格导入页要把存好的映射填回可编辑的表单里，只有 key_columns 的话，
      "按「X」分配编号"这句中文再也解析不回一条规则——用户一打开编辑器，
      那条键就会被悄悄降级成一个叫这句中文的普通列。
    """
    entities = []
    for e in config.entities:
        key_columns = []
        key_parts: list[dict] = []
        for part in e.node_key_parts:
            if isinstance(part, ColumnNodeKeyPart):
                key_columns.append(part.column)
                key_parts.append({"kind": "column", "column": part.column})
            else:
                key_columns.append(f"按「{part.raw_value_column}」分配编号")
                key_parts.append(
                    {
                        "kind": "allocated_code",
                        "scope_columns": list(part.scope_columns),
                        "raw_value_column": part.raw_value_column,
                    }
                )
        entities.append(
            {
                "term_type": e.term_type,
                "source_file": e.source_file,
                "key_columns": key_columns,
                "key_parts": key_parts,
                "name_columns": list(e.standard_name_parts),
                # 属性：{字段名: 源列}。界面按"源列 → 挂到这个实体的 字段名"展示。
                "attributes": dict(e.field_mappings),
            }
        )
    relations = [
        {
            "relation_type": r.relation_type,
            "subject_term_type": r.subject_term_type,
            "object_term_type": r.object_term_type,
        }
        for r in config.relations
    ]
    return {"entities": entities, "relations": relations}


def load_schema_etl_config(path: Path) -> SchemaETLConfig:
    return parse_schema_etl_config(path.read_text(encoding="utf-8"), origin=str(path))
