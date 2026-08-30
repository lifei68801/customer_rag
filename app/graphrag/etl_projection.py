"""ETL 三层管道的第二层：projection——把 staging 产出的源行物化成
node_key、展示名和属性值。

这一层的存在理由是"算完不立刻写"：键先成为可以检查的数据，主键重复
才可能在写入之前被发现。见
docs/superpowers/specs/2026-08-30-etl-layered-pipeline-design.md。

它也是 Foundry「一个数据集只背书一个对象类型」那条规则在本项目的落点：
一份宽事实表在这一层按 EntityMapping 被切成每个 term_type 一份，物理上
仍是一个上传文件，逻辑上已经是 1:1。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from app.graphrag.etl_staging import read_table_rows
from app.graphrag.ontology_categories import ExtraFieldSpec
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping
from app.graphrag.schema_etl_row_processing import (
    RowProcessingError,
    compute_node_key,
    convert_field_value,
)

logger = logging.getLogger(__name__)

# DuplicateNodeKeyError 的消息里最多列出多少条冲突样例——18 万行的表可能
# 有上万处冲突，全列出来会把日志和界面刷爆。
_MAX_DUPLICATE_SAMPLES = 20


@dataclass(frozen=True)
class ProjectedRow:
    """一行源数据物化之后的结果：可以直接交给写入层，不需要再看源文件。"""

    row_number: int
    node_key: str
    standard_name: str
    extra_properties: dict[str, object]


@dataclass(frozen=True)
class ProjectedRelationRow:
    """一行源数据算出的两个端点键。关系边的真实含义是"这两个值在某一行里
    同时出现过"——projection 只负责把这两个键算出来，端点在不在术语表里
    是写入层的判断。"""

    row_number: int
    subject_node_key: str
    object_node_key: str


@dataclass(frozen=True)
class RowFailure:
    """行级脏数据（缺列、类型转换失败）。语义不变：跳过 + 记报告，不中断整批。"""

    row_number: int
    reason: str


@dataclass(frozen=True)
class KeyScanResult:
    """第一遍扫描的产物。只保留键和行号——不保留行本身，内存上界因此
    只跟行数有关，跟行有多宽无关。"""

    duplicate_keys: dict[str, list[int]]
    scanned_rows: int


class DuplicateNodeKeyError(Exception):
    """一个或多个实体类型算出了重复的 node_key，整次运行失败、零写入。

    这不是"某几行数据脏"，是配置层面的错误——node_key_parts 声明的列组合
    不足以唯一标识每一行，跳过多少行都不会让配置变对。部分写入会留下一个
    "看起来成功了、实际缺了一部分"的图谱，比失败更难发现。
    """


async def scan_entity_node_keys(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    mapping: EntityMapping,
    data_dir: Path,
) -> KeyScanResult:
    """第一遍：流式读一遍源文件，只算 node_key，收集重复。

    行级失败（缺列等）在这一遍被静默忽略——它们不是键冲突，而且第二遍
    会把它们统一记录成 RowFailure，在这里记一次会重复计数。

    注意 compute_node_key 在这里仍然 allow_allocation=True，也就是说这一遍
    已经会给首次出现的原始值分配稳定码、写进 etl_stable_code_registry。
    "预检失败则零写入"这个保证对 terms 和 Neo4j 成立，对稳定码注册表不
    成立——稳定码是幂等分配的（同一 scope + 原始值永远得到同一个码），
    重跑会命中已有分配，不会漂移。
    """
    seen: dict[str, list[int]] = {}
    scanned = 0
    for row_number, row in enumerate(read_table_rows(data_dir / mapping.source_file), start=2):
        scanned += 1
        try:
            node_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.term_type,
                node_key_parts=mapping.node_key_parts, row=row,
            )
        except RowProcessingError:
            continue
        seen.setdefault(node_key, []).append(row_number)
    return KeyScanResult(
        duplicate_keys={k: v for k, v in seen.items() if len(v) > 1},
        scanned_rows=scanned,
    )


async def project_entity_rows(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    mapping: EntityMapping,
    extra_field_specs: dict[str, ExtraFieldSpec],
    data_dir: Path,
) -> AsyncIterator[ProjectedRow | RowFailure]:
    """第二遍：流式重读源文件，产出物化后的行。

    刻意不攒成 list 返回：写入层边消费边写，行数据不全量驻留内存。第一遍
    已经保证了没有重复键，这一遍只管把每一行算出来。
    """
    for row_number, row in enumerate(read_table_rows(data_dir / mapping.source_file), start=2):
        try:
            node_key = await compute_node_key(
                conn, tenant_id=tenant_id, term_type=mapping.term_type,
                node_key_parts=mapping.node_key_parts, row=row,
            )
            missing = [c for c in mapping.standard_name_parts if not row.get(c)]
            if missing:
                raise RowProcessingError(
                    f"standard_name 需要的列 {missing!r} 不存在或为空"
                )
            # 用 " / " 连接，不用冒号——冒号是 node_key 的分隔符，展示名里
            # 再用一次会让两者在日志和界面上难以区分。
            standard_name = " / ".join(row[c] for c in mapping.standard_name_parts)
            extra_properties = {
                field_name: convert_field_value(
                    extra_field_specs=extra_field_specs, field_name=field_name,
                    raw_value=row[source_column],
                )
                for field_name, source_column in mapping.field_mappings.items()
                if source_column in row and row[source_column]
            }
        except RowProcessingError as exc:
            yield RowFailure(row_number=row_number, reason=str(exc))
            continue
        yield ProjectedRow(
            row_number=row_number, node_key=node_key,
            standard_name=standard_name, extra_properties=extra_properties,
        )


async def project_relation_rows(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    mapping: RelationMapping,
    subject_entity: EntityMapping,
    object_entity: EntityMapping,
    data_dir: Path,
) -> AsyncIterator[ProjectedRelationRow | RowFailure]:
    """关系侧的 projection：流式算出每一行的两个端点键。

    两端都用 allow_allocation=False——关系路径不该分配新的稳定码，见
    compute_node_key 的说明。未命中已有分配时 compute_node_key 抛
    RowProcessingError，在这里转成 RowFailure。

    这一层没有查重：边是 MERGE 的，同一条边从多行产生是合法的，不像实体
    主键重复那样意味着配置错了。
    """
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
        except RowProcessingError as exc:
            yield RowFailure(row_number=row_number, reason=str(exc))
            continue
        yield ProjectedRelationRow(
            row_number=row_number, subject_node_key=subject_key, object_node_key=object_key,
        )


def format_duplicate_key_error(
    duplicates_by_term_type: dict[str, dict[str, list[int]]]
) -> str:
    """把汇总的重复键渲染成一条能直接定位问题的消息。

    最多列出 _MAX_DUPLICATE_SAMPLES 条样例并注明总数——18 万行的表可能有
    上万处冲突，全列出来会把管理后台的失败详情刷爆。

    这次失败发生在 run_schema_etl 创建 ETLRunReport 之前（预检阶段），
    根本不会有运行报告产出——admin_schema_etl_routes.py 的失败分支只把
    异常消息写进 error 字段，report_json 留空。所以超过展示上限的那部分
    冲突绝不能说"见运行报告"，那是个不存在的东西：改为把每个超限的
    term_type 的完整清单用 logger.error 记进服务端日志，运维能照着日志
    定位到每一个冲突的 node_key 和它的源文件行号。没超过展示上限时
    消息本身已经列全了，不重复刷日志。
    """
    lines: list[str] = []
    for term_type, duplicates in duplicates_by_term_type.items():
        total = len(duplicates)
        lines.append(
            f"实体类型 {term_type!r} 的 node_key 有 {total} 处重复，本次未写入任何数据。"
        )
        lines.append(
            "配置里 node_key_parts 声明的列组合不足以唯一标识每一行，请检查："
        )
        for node_key, row_numbers in list(duplicates.items())[:_MAX_DUPLICATE_SAMPLES]:
            rows_text = ", ".join(str(n) for n in row_numbers)
            lines.append(f"  {node_key}  ← 源文件第 {rows_text} 行")
        if total > _MAX_DUPLICATE_SAMPLES:
            lines.append(
                f"  ...仅展示前 {_MAX_DUPLICATE_SAMPLES} 处，实际共 {total} 处重复，"
                "完整清单已记入服务端日志。"
            )
            full_list = "\n".join(
                f"  {node_key}  ← 源文件第 {', '.join(str(n) for n in row_numbers)} 行"
                for node_key, row_numbers in duplicates.items()
            )
            logger.error(
                "实体类型 %r 的 node_key 重复完整清单（共 %d 处，失败消息只展示了前 %d 条）：\n%s",
                term_type, total, _MAX_DUPLICATE_SAMPLES, full_list,
            )
    return "\n".join(lines)
