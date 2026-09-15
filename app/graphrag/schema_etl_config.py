from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.graphrag.source_parse_options import (
    InvalidSourceParseOptionsError,
    SourceParseOptions,
)


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
    #: 文件名 → 这张表怎么读。没有条目的文件按 SourceParseOptions() 的缺省读。
    #: 属于 staging 层，不进 entities/relations，也不进本体。
    sources: dict[str, SourceParseOptions] = field(default_factory=dict)


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


def parse_schema_etl_config(text: str, *, origin: str = "<yaml>") -> SchemaETLConfig:
    """从 YAML 文本解析。origin 只用于报错里指出这段 YAML 来自哪儿。"""
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "tenant_id" not in data:
        raise InvalidSchemaETLConfigError(f"配置文件缺少 tenant_id: {origin}")
    return SchemaETLConfig(
        tenant_id=data["tenant_id"],
        entities=[_parse_entity_mapping(raw) for raw in data.get("entities") or []],
        relations=[_parse_relation_mapping(raw) for raw in data.get("relations") or []],
        sources=_parse_sources(data.get("sources") or []),
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

    ``sources`` 是 staging 层的解析选项（这张表怎么读），跟 entities/relations
    （读出来的列怎么映射到本体）是两回事。摘要必须带上它——表格导入页靠摘要
    回填表单，不带的话用户重开页面会看到"表头在第 1 行"，跟他上次存的不一样，
    而且没有任何提示。
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


def load_schema_etl_config(path: Path) -> SchemaETLConfig:
    return parse_schema_etl_config(path.read_text(encoding="utf-8"), origin=str(path))
