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
from app.graphrag.etl_projection import (
    DuplicateNodeKeyError,
    RowFailure,
    format_duplicate_key_error,
    project_entity_rows,
    project_relation_rows,
    scan_entity_node_keys,
)
from app.graphrag.etl_stable_code_registry import ensure_stable_code_registry_schema
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.ontology import Term
from app.graphrag.relation_writer import RelationWriterProtocol
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_constraints import list_allowed_combinations, to_combination_keys
from app.graphrag.ontology_lifecycle import is_ontology_confirmed
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.ontology_store import open_ontology_store_conn
from app.graphrag.schema_etl_config import EntityMapping, RelationMapping, SchemaETLConfig, load_schema_etl_config
from app.graphrag.schema_etl_row_processing import RowProcessingError
from app.graphrag.terms_store import (
    TermNameConflictError,
    UnknownCategoryError,
    delete_terms_by_node_keys,
    list_etl_node_keys_by_term_type,
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

    async def delete_term_node(self, *, tenant_id: str, node_key: str) -> None: ...

    async def delete_stale_relations_by_source(
        self, source: str, *, tenant_id: str, before_recorded_at: str
    ) -> int: ...


class SchemaETLNotConfirmedError(Exception):
    """该租户的本体 schema 还没有 confirm，拒绝运行 ETL——见
    docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md 第 6.2 节。"""


class DuplicateEntityMappingError(Exception):
    """config.entities 里出现了重复的 term_type，整轮失败、零写入。

    run_schema_etl 内部两处都按 term_type 建字典——
    entity_mappings_by_term_type（关系写入时查主客体实体映射）和
    scanned_keys_by_term_type（sweep 判定"源里还有哪些键"）。Python 字典
    字面量对重复 key 是静默取最后一条的，不会报错也不会警告。这不是
    "某几行数据脏"，是配置本身声明了两条互相冲突的映射，跳过多少行都不会
    让配置变对：前一条映射贡献的 node_key 集合会被后一条悄悄覆盖掉，进而
    在 sweep 判定里被当成"源里已经没有这个键"，本该保留的实体会被当成陈旧
    数据删除——这比"关系端点查找不到、关系行被跳过"更严重，是静默的数据
    丢失而不是可见的失败，所以必须在任何写入之前就整体拒绝，走法与
    DuplicateNodeKeyError 一致。"""


class SweepSafetyValveError(Exception):
    """源端删除的清理规模超过安全阈值，整轮失败、零改动。

    一次误传的、被截断的源文件会静默清空大半个图谱，而症状要等用户提问
    答不出来才暴露。阈值和放行开关让"我确实要缩减数据"这件事必须被显式
    表达。阈值是启发式而不是正确性保证——它拦不住 49% 的误删，作用是把
    最常见的事故形态（传错文件、导出被截断）挡在门外。
    """


# 单个 term_type 的清理占比超过这个比例就触发安全阀。50% 是拍的，没有
# 数据依据——真实租户的数据波动幅度未知，可能过松也可能过紧，跑过若干
# 次真实运行后应当回头调整。
_SWEEP_SAFETY_THRESHOLD = 0.5


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
    # 源端删除的传播（2026-08-31）。零删除时这三个字段也会出现在报告里——
    # "本次没有移除任何实体"和"根本没跑删除逻辑"必须能区分开。
    entities_removed: int = 0
    entities_removed_by_type: dict[str, int] = field(default_factory=dict)
    # dry_run=True 时这个值不代表预测——关系侧无法预演。原因：dry-run 从不
    # 进入关系写入，扫除条件"recorded_at < 本轮时间戳"会匹配该 source 下
    # 全部现存边（没有任何边在本轮被重写、刷新时间戳）；如果照实体侧的方式
    # 朴素统计"现存边有多少条"，报出来的会是"将删除全部关系"，比不报更
    # 误导。要正确预览关系侧，需要先投影出本轮会写哪些边、再和现存边做
    # 差集，那是另一个设计，不在这次范围内——所以 dry-run 下这个字段固定
    # 是 0，且这个 0 不代表"没有要删的关系"。
    relations_removed: int = 0
    # dry_run=True 时，entities_removed / entities_removed_by_type 是
    # "将要删除多少"（预演，terms 和 Neo4j 都零写入）；dry_run=False 时是
    # "已经删了多少"（delete_terms_by_node_keys 的真实返回值）。
    dry_run: bool = False


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
    sweep_by_term_type: dict[str, set[str]],
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
    # 扣掉本轮已经判定要 sweep 掉的键：关系写入发生在实体 sweep 之前
    # （sweep 的执行必须放在全部写入之后，否则会在中途留下实体缺失，见
    # run_schema_etl 里 sweep 循环前的说明），所以上面两次预取查到的是
    # sweep 执行前的 SQLite 状态——一个端点即使本轮源里已经没有它、注定要
    # 被删，只要它是上一轮写的旧行，此刻仍然"存在"，会让守卫误判成"端点
    # 还在"而放行。放行的后果是：这一行关系被写进图谱，紧接着 sweep 用
    # delete_term_node（DETACH DELETE）把这个刚写的端点连同这条边一起删掉
    # ——relations_written 已经计过数，边却不存在了，写完立刻删这种不自洽
    # 正是这条分支要消除的。所以这里不问"SQLite 里还有没有这一行"，而是问
    # "本轮判定源里已经消失了吗"：命中 doomed 集合的键，从"已知存在"的键
    # 集里去掉，让它们走下面既有的"端点不存在"分支，被跳过而不是被写入。
    subject_doomed = sweep_by_term_type.get(mapping.subject_term_type, set())
    object_doomed = sweep_by_term_type.get(mapping.object_term_type, set())
    subject_node_keys = subject_node_keys - subject_doomed
    object_node_keys = object_node_keys - object_doomed

    # 这一层不再自己读文件、不再自己算键——那两件事已经在 projection 层
    # 做完了（见 etl_projection.py）。这里只负责端点存在性校验和写入。
    async for projected in project_relation_rows(
        conn, tenant_id=tenant_id, mapping=mapping,
        subject_entity=subject_entity, object_entity=object_entity, data_dir=data_dir,
    ):
        if isinstance(projected, RowFailure):
            report.relations_skipped += 1
            _record_skipped_row(
                report, label=mapping.relation_type, source_file=mapping.source_file,
                row_number=projected.row_number, reason=projected.reason,
            )
            continue
        try:
            # 端点存在性守卫留在写入层：它需要预取的 node_key 集合，而且
            # 语义是"写入时刻这个端点在不在术语表里、且不会在本轮被 sweep
            # 掉"，不是 projection 能回答的。守卫的判定逻辑本身没变——
            # merge_relation 的两端都是 MERGE，node_key 对不上任何已有节点
            # 时不会报错，而是凭空建出一个只有 tenant_id/node_key 的幽灵
            # 节点；变的是 known_keys 集合的内容（已扣掉 doomed，见上）。
            for key, known_keys, term_type in (
                (projected.subject_node_key, subject_node_keys, mapping.subject_term_type),
                (projected.object_node_key, object_node_keys, mapping.object_term_type),
            ):
                if key not in known_keys:
                    raise RowProcessingError(
                        f"关系端点 {key!r} 在术语表里不存在"
                        f"（{term_type!r} 的实体行可能被跳过、尚未写入，或本轮已被判定"
                        f"为源里消失、即将被清理）"
                    )
            await graph_client.merge_relation(
                subject_standard_name=projected.subject_node_key,
                object_standard_name=projected.object_node_key,
                relation_type=mapping.relation_type, source=mapping.source_file,
                tenant_id=tenant_id, provenance=provenance.ETL, recorded_at=recorded_at,
            )
            report.relations_written += 1
            _record_written(report, label=mapping.relation_type)
        except RowProcessingError as exc:
            report.relations_skipped += 1
            _record_skipped_row(
                report, label=mapping.relation_type, source_file=mapping.source_file,
                row_number=projected.row_number, reason=str(exc),
            )


async def run_schema_etl(
    *,
    conn: aiosqlite.Connection,
    graph_client: SchemaEtlGraphProtocol,
    config: SchemaETLConfig,
    data_dir: Path,
    dry_run: bool = False,
    allow_large_sweep: bool = False,
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

    # 预检最先做的一件事：config.entities 里 term_type 不能重复。下面的
    # scanned_keys_by_term_type 和后面的 entity_mappings_by_term_type 都
    # 按 term_type 建字典，重复声明会被静默折叠成后一条——见
    # DuplicateEntityMappingError 的文档字符串。这一步不依赖 term_type
    # 是否在已确认 schema 里，纯粹是 config 自身的结构性检查，所以放在
    # confirmed_term_type_values 计算之前，任何扫描、任何写入之前。
    term_type_source_files: dict[str, list[str]] = {}
    for entity_mapping in config.entities:
        term_type_source_files.setdefault(entity_mapping.term_type, []).append(
            entity_mapping.source_file
        )
    duplicate_term_types = {
        term_type: source_files
        for term_type, source_files in term_type_source_files.items()
        if len(source_files) > 1
    }
    if duplicate_term_types:
        detail = "；".join(
            f"{term_type!r} 出现在 {source_files!r}"
            for term_type, source_files in duplicate_term_types.items()
        )
        raise DuplicateEntityMappingError(
            f"config.entities 里以下 term_type 被声明了不止一次，本次未做任何改动：{detail}"
        )

    # 预检：所有实体映射先各扫一遍键，确认没有重复，才进入写入。
    #
    # 为什么整体失败而不是逐行跳过：主键重复意味着这份配置的 node_key_parts
    # 选错了——它没能唯一标识每一行。这不是"某几行数据脏"，跳过多少行都不
    # 会让配置变对。部分写入反而留下一个"看起来成功了、实际缺了一部分"的
    # 图谱，比失败更难发现。见 2026-08-30-etl-layered-pipeline-design.md。
    #
    # 这一遍不「校验」term_type——term_type 打错字仍然只由 _write_entity_mapping
    # 负责判定，失败仍然记进 skipped_mappings，预检不会替它抢先 raise。但预检
    # 必须先看一眼 term_type 在不在已确认 schema 里，跳过不在里面的 mapping、
    # 不去扫它的键：scan_entity_node_keys 内部的 compute_node_key
    # (allow_allocation=True) 会为 AllocatedCodeNodeKeyPart 真实分配并持久化
    # 稳定码，它不关心 term_type 合不合法。重构前 term_type 校验在
    # _write_entity_mapping 的行循环之前，打错字的 mapping 从不触发
    # compute_node_key；现在预检抢在它之前扫了一遍键，如果不加这层判断，
    # 一个 term_type 打错字的 mapping 就会往 etl_stable_code_registry 里
    # 写入永远不会被业务数据引用的孤儿码——这是相对重构前的真实行为倒退。
    confirmed_term_type_values = {
        t.value for t in await list_term_types(conn, config.tenant_id, status="confirmed")
    }
    duplicates_by_term_type: dict[str, dict[str, list[int]]] = {}
    scanned_keys_by_term_type: dict[str, set[str]] = {}
    for entity_mapping in config.entities:
        if entity_mapping.term_type not in confirmed_term_type_values:
            continue
        try:
            scan = await scan_entity_node_keys(
                conn, tenant_id=config.tenant_id, mapping=entity_mapping, data_dir=data_dir,
            )
        except RowProcessingError:
            # 文件类型不支持之类的问题，留给写入阶段按老路径记进
            # skipped_mappings，预检不抢着报错。
            continue
        if scan.duplicate_keys:
            duplicates_by_term_type[entity_mapping.term_type] = scan.duplicate_keys
        scanned_keys_by_term_type[entity_mapping.term_type] = scan.node_keys
    if duplicates_by_term_type:
        raise DuplicateNodeKeyError(format_duplicate_key_error(duplicates_by_term_type))

    # sweep 集合在这里就能算出来——预检第一遍已经持有本次源文件的全部
    # node_key。因此安全阀的判定发生在任何写入之前，"整轮零改动"是结构性
    # 的，跟 DuplicateNodeKeyError 走同一条路径，不是靠记得回滚。
    #
    # 只圈 source='etl' 的行：审核界面创建的（'review'）和管理后台手工录入
    # 的（'manual'）从来就不来自这个数据源，"源里没有"对它们不成立。
    sweep_by_term_type: dict[str, set[str]] = {}
    existing_etl_keys_by_term_type: dict[str, set[str]] = {}
    for term_type, scanned_keys in scanned_keys_by_term_type.items():
        existing = await list_etl_node_keys_by_term_type(conn, config.tenant_id, term_type)
        existing_etl_keys_by_term_type[term_type] = existing
        sweep_by_term_type[term_type] = existing - scanned_keys

    if not allow_large_sweep:
        for term_type, doomed in sweep_by_term_type.items():
            existing_count = len(existing_etl_keys_by_term_type[term_type])
            if existing_count == 0 or not doomed:
                continue
            ratio = len(doomed) / existing_count
            if ratio > _SWEEP_SAFETY_THRESHOLD:
                raise SweepSafetyValveError(
                    f"实体类型 {term_type!r} 的清理将移除 {len(doomed)} / {existing_count} 行"
                    f"（{ratio:.0%}），超过安全阈值 {_SWEEP_SAFETY_THRESHOLD:.0%}，本次未做任何改动。\n"
                    f"如果源文件确实缩减到这个规模，勾选\"允许大规模清理\"后重跑。"
                )

    if dry_run:
        # 预演：把将要删除的实体规模填进报告就返回。首次启用 sweep 时历史
        # 累积的孤儿实体可能规模不小，让租户先看一眼。
        #
        # 准确的说法是"terms 和 Neo4j 零写入"，不是"零副作用"：预检里的
        # compute_node_key(allow_allocation=True)（在上面 scan_entity_node_keys
        # 内部）会为首次出现的原始值分配并持久化稳定码，写 etl_stable_code_
        # registry 表。这个副作用本身无害——稳定码幂等分配，同一 scope +
        # 原始值永远得到同一个码，重复 dry-run 或紧接着的正式跑不会漂移——
        # 但如果说成"不删除、不写入"就盖过了这一点，故意把话说准确。
        #
        # 只覆盖实体侧：这里从不进入关系写入/清理，所以 relations_removed
        # 固定是 0，且这个 0 不代表"关系侧没有要删的"——见 ETLRunReport.
        # relations_removed 字段注释里的完整解释。
        preview = ETLRunReport(dry_run=True)
        preview.entities_removed = sum(len(v) for v in sweep_by_term_type.values())
        preview.entities_removed_by_type = {
            term_type: len(doomed) for term_type, doomed in sweep_by_term_type.items()
        }
        return preview

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
                data_dir=data_dir, report=report, sweep_by_term_type=sweep_by_term_type,
            )
        except RowProcessingError as exc:
            report.skipped_mappings.append(
                SkippedMapping(
                    label=relation_mapping.relation_type, source_file=relation_mapping.source_file, reason=str(exc),
                )
            )

    # 关系用"先写后扫"：本轮该写的边都已 MERGE 完（时间戳被刷新成本轮的
    # 值），现在删掉同源下时间戳更早的——那些就是上一轮写过、这一轮源里
    # 已经没有的边。任何时刻图谱都是完整的，中途失败最多留下新旧共存，
    # 下次重跑自愈；若改成"先全删再全写"，中途失败会留下一个边被删光、
    # 实体还在的图谱，而 ETL 数据量大、这个窗口很长。
    #
    # 按源文件去重：多条关系映射常常共享同一个源文件（demo 配置里五条关系
    # 全部来自 soft_drink_sales.xlsx），逐映射扫一遍是重复劳动，
    # dict.fromkeys 去重的同时保持顺序。
    #
    # recorded_at 传给 merge_relation 时由 neo4j_client 内部做 strftime；
    # 这里必须自己格式化成完全一样的字符串，否则字符串比较会错——见
    # delete_stale_relations_by_source 的说明。
    recorded_at_text = recorded_at.strftime("%Y-%m-%d %H:%M:%S")
    for source_file in dict.fromkeys(m.source_file for m in config.relations):
        report.relations_removed += await graph_client.delete_stale_relations_by_source(
            source_file, tenant_id=config.tenant_id, before_recorded_at=recorded_at_text,
        )

    # sweep 的执行放在写入之后：判定必须在写入前（才能保证阀触发时零改动），
    # 但执行必须在写入后——先删后写会在中途留下实体缺失，关系写入的端点
    # 存在性守卫会大面积误判、把合法的关系行全部跳过。
    #
    # 双存储内部的删除顺序：每个 term_type 都先删 Neo4j 节点、再删 SQLite
    # 行——这个顺序不是随手排的，是"哪个方向能自愈"决定的。doomed 集合
    # 本身算自 SQLite 的 source='etl' 行（scanned_keys_by_term_type 之前
    # 那一段）；如果反过来先删 SQLite（批量 DELETE，内部已 commit）、
    # 再逐节点删 Neo4j，一旦 delete_term_node 中途抛异常：SQLite 行已经
    # 没了，Neo4j 节点还在，而这个孤儿节点的 node_key 已经不在任何一次
    # 未来 sweep 的候选集里（候选集来自 SQLite）——重跑也救不回来，是
    # 不可自愈的方向。现在这个顺序下，同样中途失败：SQLite 还完好，
    # delete_term_node 已经处理过的那些节点在 Neo4j 里也已经没了，但
    # 下次重跑会重新算出同一个 doomed 集合、对着这些节点再调一次
    # delete_term_node——MATCH 匹配不到就是空操作，天然幂等——直到全部
    # 处理完才会执行 SQLite 侧的批量删除。这个方向最终收敛，反方向不会。
    for term_type, doomed in sweep_by_term_type.items():
        if not doomed:
            # 零删除也要出现在报告里——"本次没有移除任何实体"和"根本没
            # 跑删除逻辑"必须能区分开，见 ETLRunReport 上的字段注释。
            report.entities_removed_by_type[term_type] = 0
            continue
        for node_key in doomed:
            # delete_term_node 是 DETACH DELETE，连这个节点的边和别名节点
            # 一起清掉，不会留下悬空引用。
            await graph_client.delete_term_node(
                tenant_id=config.tenant_id, node_key=node_key
            )
        # 两个字段都用 delete_terms_by_node_keys 的真实返回值，而不是
        # len(doomed)（计划要删多少）——两者语义不同，理论上可能分叉，
        # 报告里应该反映实际发生了什么，不是预期发生了什么。
        removed = await delete_terms_by_node_keys(conn, config.tenant_id, doomed)
        report.entities_removed += removed
        report.entities_removed_by_type[term_type] = removed

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
