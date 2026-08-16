from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiosqlite

from app.config.settings import Settings
from app.graphrag import provenance
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.review_factory import build_review_conn_from_settings
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping, SchemaETLConfig, load_schema_etl_config
from app.graphrag.schema_etl_row_processing import RowProcessingError, compute_node_key, convert_field_value
from app.graphrag.terms_store import TermNameConflictError, get_term, upsert_term_with_node_key


class SchemaETLNotConfirmedError(Exception):
    """该租户的本体 schema 还没有 confirm，拒绝运行 ETL——见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 6.2 节。"""


@dataclass
class SkippedRow:
    source_file: str
    row_number: int
    reason: str


@dataclass
class ETLRunReport:
    entities_written: int = 0
    entities_skipped: int = 0
    relations_written: int = 0
    relations_skipped: int = 0
    skipped_rows: list[SkippedRow] = field(default_factory=list)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


async def _write_entity_mapping(
    *,
    conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    tenant_id: str,
    mapping: EntityMapping,
    data_dir: Path,
    report: ETLRunReport,
) -> None:
    term_types = await list_term_types(conn, tenant_id)
    types_by_value = {t.value: t for t in term_types}
    if mapping.term_type not in types_by_value:
        raise RowProcessingError(f"term_type {mapping.term_type!r} 不在已确认 schema 里")
    extra_field_specs = {f.name: f for f in types_by_value[mapping.term_type].extra_fields}

    rows = _read_csv_rows(data_dir / mapping.source_file)
    for row_number, row in enumerate(rows, start=2):  # 第 1 行是表头
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
        except (RowProcessingError, TermNameConflictError) as exc:
            report.entities_skipped += 1
            report.skipped_rows.append(
                SkippedRow(source_file=mapping.source_file, row_number=row_number, reason=str(exc))
            )


async def _write_relation_mapping(
    *,
    conn: aiosqlite.Connection,
    graph_client: Neo4jGraphClient,
    tenant_id: str,
    mapping: RelationMapping,
    entity_mappings_by_term_type: dict[str, EntityMapping],
    data_dir: Path,
    report: ETLRunReport,
) -> None:
    subject_entity = entity_mappings_by_term_type.get(mapping.subject_term_type)
    object_entity = entity_mappings_by_term_type.get(mapping.object_term_type)
    if subject_entity is None or object_entity is None:
        raise RowProcessingError(
            f"关系 {mapping.relation_type!r} 引用的实体类型未在 entities 段声明"
        )
    rows = _read_csv_rows(data_dir / mapping.source_file)
    for row_number, row in enumerate(rows, start=2):
        try:
            subject_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.subject_term_type,
                node_key_parts=subject_entity.node_key_parts, row=row,
            )
            object_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.object_term_type,
                node_key_parts=object_entity.node_key_parts, row=row,
            )
            await graph_client.merge_relation(
                subject_standard_name=subject_key, object_standard_name=object_key,
                relation_type=mapping.relation_type, source=mapping.source_file,
                tenant_id=tenant_id, provenance=provenance.ETL, recorded_at=datetime.now(),
            )
            report.relations_written += 1
        except RowProcessingError as exc:
            report.relations_skipped += 1
            report.skipped_rows.append(
                SkippedRow(source_file=mapping.source_file, row_number=row_number, reason=str(exc))
            )


async def run_schema_etl(
    *, conn: aiosqlite.Connection, graph_client: Neo4jGraphClient, config: SchemaETLConfig, data_dir: Path
) -> ETLRunReport:
    """按已确认 schema + 列映射配置，把 CSV 源数据确定性写入 Term/Neo4j 双存储。
    见 docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 6 节。
    """
    if not await is_ontology_confirmed(conn, config.tenant_id):
        raise SchemaETLNotConfirmedError(
            f"租户 {config.tenant_id!r} 的本体 schema 还没有确认，拒绝运行 ETL"
        )
    await ensure_stable_code_registry_schema(conn)

    report = ETLRunReport()
    for entity_mapping in config.entities:
        await _write_entity_mapping(
            conn=conn, graph_client=graph_client, tenant_id=config.tenant_id,
            mapping=entity_mapping, data_dir=data_dir, report=report,
        )

    entity_mappings_by_term_type = {m.term_type: m for m in config.entities}
    for relation_mapping in config.relations:
        await _write_relation_mapping(
            conn=conn, graph_client=graph_client, tenant_id=config.tenant_id,
            mapping=relation_mapping, entity_mappings_by_term_type=entity_mappings_by_term_type,
            data_dir=data_dir, report=report,
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
    for skipped in report.skipped_rows:
        print(f"  跳过 {skipped.source_file} 第 {skipped.row_number} 行：{skipped.reason}")


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(_main(config_path=args.config, data_dir=args.data_dir))
