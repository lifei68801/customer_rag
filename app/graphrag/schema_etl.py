from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import aiosqlite

from app.config.settings import Settings
from app.graphrag import provenance
from app.graphrag.etl_projection import RowFailure, project_entity_rows
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.etl_staging import read_table_rows
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.ontology import Term
from app.graphrag.relation_writer import RelationWriterProtocol
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations, to_combination_keys
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.ontology_store import open_ontology_store_conn
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping, SchemaETLConfig, load_schema_etl_config
from app.graphrag.schema_etl_row_processing import RowProcessingError, compute_node_key
from app.graphrag.terms_store import (
    TermNameConflictError,
    UnknownCategoryError,
    list_node_keys_by_term_type,
    upsert_term_with_node_key,
)


class SchemaEtlGraphProtocol(RelationWriterProtocol, Protocol):
    """ETL 引擎实际调用的两个图写方法——独立声明，不继承 admin 路由用的
    GraphWriteProtocol（neo4j_client.py）或摄取管道用的
    GraphWriteClientProtocol（normalization.py）：三者是不同消费方，各自
    只暴露自己真正用到的方法，见 2026-08-27 架构评审对 GraphClientProtocol
    过宽问题的讨论。merge_relation 的签名继承自 RelationWriterProtocol——
    窄协议的意图不变，只是那份签名不再各抄一遍。"""

    async def sync_term(self, term: Term) -> None: ...


class SchemaETLNotConfirmedError(Exception):
    """该租户的本体 schema 还没有 confirm，拒绝运行 ETL——见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 6.2 节。"""


@dataclass
class SkippedRow:
    label: str
    source_file: str
    row_number: int
    reason: str


@dataclass
class SkippedMapping:
    label: str
    source_file: str
    reason: str


@dataclass
class ETLRunReport:
    entities_written: int = 0
    entities_skipped: int = 0
    relations_written: int = 0
    relations_skipped: int = 0
    written_by_type: dict[str, int] = field(default_factory=dict)
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    skipped_rows: list[SkippedRow] = field(default_factory=list)
    skipped_mappings: list[SkippedMapping] = field(default_factory=list)


def _record_written(report: ETLRunReport, *, label: str) -> None:
    report.written_by_type[label] = report.written_by_type.get(label, 0) + 1


def _record_skipped_row(
    report: ETLRunReport, *, label: str, source_file: str, row_number: int, reason: str
) -> None:
    report.skipped_by_type[label] = report.skipped_by_type.get(label, 0) + 1
    report.skipped_rows.append(
        SkippedRow(label=label, source_file=source_file, row_number=row_number, reason=reason)
    )


async def _write_entity_mapping(
    *,
    conn: aiosqlite.Connection,
    graph_client: SchemaEtlGraphProtocol,
    tenant_id: str,
    mapping: EntityMapping,
    data_dir: Path,
    report: ETLRunReport,
) -> None:
    term_types = await list_term_types(conn, tenant_id, status="confirmed")
    types_by_value = {t.value: t for t in term_types}
    if mapping.term_type not in types_by_value:
        raise RowProcessingError(f"term_type {mapping.term_type!r} 不在已确认 schema 里")
    extra_field_specs = {f.name: f for f in types_by_value[mapping.term_type].extra_fields}

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


async def _write_relation_mapping(
    *,
    conn: aiosqlite.Connection,
    graph_client: SchemaEtlGraphProtocol,
    tenant_id: str,
    mapping: RelationMapping,
    entity_mappings_by_term_type: dict[str, EntityMapping],
    confirmed_relation_types: set[str],
    allowed_combinations: set[tuple[str, str, str]],
    recorded_at: datetime,
    data_dir: Path,
    report: ETLRunReport,
) -> None:
    if mapping.relation_type not in confirmed_relation_types:
        raise RowProcessingError(f"relation_type {mapping.relation_type!r} 不在已确认 schema 里")
    # relation_type 单独合法不代表 (subject_term_type, relation_type,
    # object_term_type) 这个组合也在已确认的允许列表里——两者是独立声明的
    # 字段，映射配置本身不保证组合有效。这条校验让 ETL 写入路径追平
    # graph_extraction.py/review_queue.py 已经在做的同一种组合校验，见
    # docs/superpowers/specs/2026-08-19-data-entry-unification-design.md
    # "不在本次范围内"第 1 条的后续处理。
    combo = (mapping.subject_term_type, mapping.relation_type, mapping.object_term_type)
    if combo not in allowed_combinations:
        raise RowProcessingError(
            f"关系类型/实体类型组合不在已确认允许列表里: "
            f"({mapping.subject_term_type!r}, {mapping.relation_type!r}, {mapping.object_term_type!r})"
        )
    subject_entity = entity_mappings_by_term_type.get(mapping.subject_term_type)
    object_entity = entity_mappings_by_term_type.get(mapping.object_term_type)
    if subject_entity is None or object_entity is None:
        raise RowProcessingError(
            f"关系 {mapping.relation_type!r} 引用的实体类型未在 entities 段声明"
        )

    # 端点实体必须真的写进术语表过，否则跳过这一行——merge_relation 的两端
    # 都是 MERGE，node_key 对不上任何已有节点时不会报错，而是凭空建出一个
    # 只有 tenant_id/node_key、没有 type/standard_name 的幽灵节点（见
    # neo4j_client.py::merge_relation 的说明）。AllocatedCodeNodeKeyPart
    # 早就有这个守卫（稳定码查不到就跳过），ColumnNodeKeyPart 一直没有：
    # 实体行被跳过（唯一索引冲突、类型转换失败、缺列）时，关系行照写不误。
    # demo 租户就这样留下了 `类目:Coffee` 和 `销量:1000` 两个幽灵节点、
    # 挂着 16 条边。run_schema_etl 保证全部实体映射先于关系映射执行，所以
    # 这里预取一次就够，不会漏掉后面才写入的实体。
    subject_node_keys = await list_node_keys_by_term_type(
        conn, tenant_id, mapping.subject_term_type
    )
    object_node_keys = await list_node_keys_by_term_type(
        conn, tenant_id, mapping.object_term_type
    )

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
            for key, known_keys, term_type in (
                (subject_key, subject_node_keys, mapping.subject_term_type),
                (object_key, object_node_keys, mapping.object_term_type),
            ):
                if key not in known_keys:
                    raise RowProcessingError(
                        f"关系端点 {key!r} 在术语表里不存在"
                        f"（{term_type!r} 的实体行可能被跳过或尚未写入）"
                    )
            await graph_client.merge_relation(
                subject_standard_name=subject_key, object_standard_name=object_key,
                relation_type=mapping.relation_type, source=mapping.source_file,
                tenant_id=tenant_id, provenance=provenance.ETL, recorded_at=recorded_at,
            )
            report.relations_written += 1
            _record_written(report, label=mapping.relation_type)
        except RowProcessingError as exc:
            report.relations_skipped += 1
            _record_skipped_row(
                report, label=mapping.relation_type, source_file=mapping.source_file,
                row_number=row_number, reason=str(exc),
            )


async def run_schema_etl(
    *, conn: aiosqlite.Connection, graph_client: SchemaEtlGraphProtocol, config: SchemaETLConfig, data_dir: Path
) -> ETLRunReport:
    """按已确认 schema + 列映射配置，把 CSV 源数据确定性写入 Term/Neo4j 双存储。
    见 docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 6 节。

    单个 mapping 级别的前置校验失败（term_type/relation_type 不在已确认 schema
    里、关系引用了未声明的实体类型）不会中断整次运行——跳过这一个 mapping、
    记入 skipped_mappings，继续处理其余 mapping，呼应第 6.4 节"一行脏数据不该
    让整批任务失败"的同一原则，只是粒度提升到了整个 mapping。
    """
    if not await is_ontology_confirmed(conn, config.tenant_id):
        raise SchemaETLNotConfirmedError(
            f"租户 {config.tenant_id!r} 的本体 schema 还没有确认，拒绝运行 ETL"
        )
    await ensure_stable_code_registry_schema(conn)

    recorded_at = datetime.now()
    report = ETLRunReport()

    for entity_mapping in config.entities:
        try:
            await _write_entity_mapping(
                conn=conn, graph_client=graph_client, tenant_id=config.tenant_id,
                mapping=entity_mapping, data_dir=data_dir, report=report,
            )
        except RowProcessingError as exc:
            report.skipped_mappings.append(
                SkippedMapping(
                    label=entity_mapping.term_type, source_file=entity_mapping.source_file, reason=str(exc),
                )
            )

    confirmed_relation_types = {
        r.relation_type for r in await list_relation_types(conn, config.tenant_id, status="confirmed")
    }
    allowed_combinations = to_combination_keys(
        await list_allowed_combinations(conn, config.tenant_id, status="confirmed")
    )
    entity_mappings_by_term_type = {m.term_type: m for m in config.entities}
    for relation_mapping in config.relations:
        try:
            await _write_relation_mapping(
                conn=conn, graph_client=graph_client, tenant_id=config.tenant_id,
                mapping=relation_mapping, entity_mappings_by_term_type=entity_mappings_by_term_type,
                confirmed_relation_types=confirmed_relation_types,
                allowed_combinations=allowed_combinations, recorded_at=recorded_at,
                data_dir=data_dir, report=report,
            )
        except RowProcessingError as exc:
            report.skipped_mappings.append(
                SkippedMapping(
                    label=relation_mapping.relation_type, source_file=relation_mapping.source_file, reason=str(exc),
                )
            )

    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按列映射配置把结构化 CSV 数据写入知识图谱")
    parser.add_argument("--config", required=True, type=Path, help="列映射 YAML 配置文件路径")
    parser.add_argument("--data-dir", required=True, type=Path, help="配置里 source_file 相对路径的基准目录")
    return parser.parse_args()


async def _main(*, config_path: Path, data_dir: Path) -> None:
    settings = Settings()
    config = load_schema_etl_config(config_path)
    conn = await open_ontology_store_conn(settings)
    graph_client = build_graph_client_from_settings(settings)
    try:
        report = await run_schema_etl(conn=conn, graph_client=graph_client, config=config, data_dir=data_dir)
    finally:
        await conn.close()
    print(
        f"实体写入 {report.entities_written} 条，跳过 {report.entities_skipped} 条；"
        f"关系写入 {report.relations_written} 条，跳过 {report.relations_skipped} 条"
    )
    for skipped in report.skipped_mappings:
        print(f"  跳过整个映射 {skipped.label}（{skipped.source_file}）：{skipped.reason}")
    for skipped in report.skipped_rows:
        print(f"  跳过 {skipped.label} / {skipped.source_file} 第 {skipped.row_number} 行：{skipped.reason}")


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(_main(config_path=args.config, data_dir=args.data_dir))
