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
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.review_factory import build_review_conn_from_settings
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping, SchemaETLConfig, load_schema_etl_config
from app.graphrag.schema_etl_row_processing import RowProcessingError, compute_node_key, convert_field_value
from app.graphrag.terms_store import TermNameConflictError, UnknownCategoryError, upsert_term_with_node_key


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


def _read_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    """逐行流式产出源文件的行，不把整个文件读进内存——设计文档第 6.4 节
    给出的真实规模是"MUJI 一张 SKU 表 18 万+ 行"。"""
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


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
    graph_client: Neo4jGraphClient,
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

    for row_number, row in enumerate(_read_csv_rows(data_dir / mapping.source_file), start=2):  # 第 1 行是表头
        try:
            node_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.term_type,
                node_key_parts=mapping.node_key_parts, row=row,
            )
            if not row.get(mapping.standard_name_column):
                raise RowProcessingError(f"standard_name 需要的列 {mapping.standard_name_column!r} 不存在或为空")
            standard_name = row[mapping.standard_name_column]
            extra_properties = {
                field_name: convert_field_value(
                    extra_field_specs=extra_field_specs, field_name=field_name,
                    raw_value=row[source_column],
                )
                for field_name, source_column in mapping.field_mappings.items()
                if source_column in row and row[source_column]
            }
            await upsert_term_with_node_key(
                conn, tenant_id=tenant_id, node_key=node_key, standard_name=standard_name,
                aliases=[], term_type=mapping.term_type, product_line=mapping.product_line,
                extra_properties=extra_properties,
            )
            term = Term(
                tenant_id=tenant_id, node_key=node_key, standard_name=standard_name,
                aliases=[], term_type=mapping.term_type, product_line=mapping.product_line,
                extra_properties=extra_properties,
            )
            await graph_client.sync_term(term)
            report.entities_written += 1
            _record_written(report, label=mapping.term_type)
        except (RowProcessingError, TermNameConflictError, UnknownCategoryError) as exc:
            report.entities_skipped += 1
            _record_skipped_row(
                report, label=mapping.term_type, source_file=mapping.source_file,
                row_number=row_number, reason=str(exc),
            )


async def _write_relation_mapping(
    *,
    conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    tenant_id: str,
    mapping: RelationMapping,
    entity_mappings_by_term_type: dict[str, EntityMapping],
    confirmed_relation_types: set[str],
    recorded_at: datetime,
    data_dir: Path,
    report: ETLRunReport,
) -> None:
    if mapping.relation_type not in confirmed_relation_types:
        raise RowProcessingError(f"relation_type {mapping.relation_type!r} 不在已确认 schema 里")
    subject_entity = entity_mappings_by_term_type.get(mapping.subject_term_type)
    object_entity = entity_mappings_by_term_type.get(mapping.object_term_type)
    if subject_entity is None or object_entity is None:
        raise RowProcessingError(
            f"关系 {mapping.relation_type!r} 引用的实体类型未在 entities 段声明"
        )

    for row_number, row in enumerate(_read_csv_rows(data_dir / mapping.source_file), start=2):
        try:
            subject_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.subject_term_type,
                node_key_parts=subject_entity.node_key_parts, row=row, allow_allocation=False,
            )
            object_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.object_term_type,
                node_key_parts=object_entity.node_key_parts, row=row, allow_allocation=False,
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
    *, conn: aiosqlite.Connection, graph_client: Neo4jGraphClient, config: SchemaETLConfig, data_dir: Path
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
    entity_mappings_by_term_type = {m.term_type: m for m in config.entities}
    for relation_mapping in config.relations:
        try:
            await _write_relation_mapping(
                conn=conn, graph_client=graph_client, tenant_id=config.tenant_id,
                mapping=relation_mapping, entity_mappings_by_term_type=entity_mappings_by_term_type,
                confirmed_relation_types=confirmed_relation_types, recorded_at=recorded_at,
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
    conn = await build_review_conn_from_settings(settings)
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
