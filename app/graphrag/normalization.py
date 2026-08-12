from __future__ import annotations

import difflib
import logging
from datetime import datetime
from typing import Any, Protocol

import aiosqlite

from app.graphrag.ontology import Term
from app.graphrag.provenance import AUTO_MERGED
from app.graphrag.review_queue import enqueue_for_review

logger = logging.getLogger(__name__)


class GraphWriteClientProtocol(Protocol):
    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
        provenance: str,
        recorded_at: datetime,
    ) -> None: ...

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None: ...


def resolve_to_standard_name(name: str, terms: list[Term]) -> str | None:
    """精确匹配单个候选实体名到术语表标准名，未命中返回 None。

    这里用精确匹配（等于标准名或某个别名），而非 term_matcher 的子串
    包含匹配——候选名来自 LLM 抽取，通常已经是较短的实体名，用更严格
    的精确匹配降低误对齐风险。
    """
    for term in terms:
        if name == term.standard_name or name in term.aliases:
            return term.standard_name
    return None


def find_fuzzy_candidate_standard_name(
    name: str, terms: list[Term], *, threshold: float = 0.75
) -> str | None:
    """精确匹配失败后的模糊匹配兜底：找相似度最高的单一标准名建议。

    和 term_matcher.py::match_terms()（TermGuard）"任意命中就收集一组
    术语"不同——那边是往上下文里塞信息，多塞几个无妨；这里要给人工审核
    一个具体的对齐建议，必须是单一最优解。因为 name 本身就是 LLM 抽取
    出的完整候选实体名（不是需要在长文本里扫描的段落），直接整串比较，
    不需要滑动窗口。

    threshold 默认 0.75（沿用 TermGuard 的保守取值）——这是参考起点，
    需要结合真实数据调整，不是权威值。返回值只是"建议"，调用方（见
    normalize_and_write_relations）不会拿这个结果自动写入图谱，而是
    连同建议一起进人工审核队列，由人工最终确认。
    """
    best_name: str | None = None
    best_ratio = 0.0
    for term in terms:
        for candidate in [term.standard_name, *term.aliases]:
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, name, candidate).ratio()
            if ratio >= threshold and ratio > best_ratio:
                best_ratio = ratio
                best_name = term.standard_name
    return best_name


async def normalize_and_write_relations(
    relations: list[dict[str, str]],
    *,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    tenant_id: str,
    now: datetime,
    review_conn: aiosqlite.Connection | None = None,
) -> int:
    """候选关系归一化对齐术语表后写入图谱，返回成功写入数。

    任一侧未能对齐标准术语、或关系类型不合法的候选不会自动入库。
    review_conn 为可选项：
    - 不传（默认）：候选只记日志后丢弃，保持阶段3落地时的行为不变；
    - 传入：候选改为写入持久化的人工待审核队列（见 review_queue.py），
      而不是随日志一起消失——对应架构文档"低置信度新实体进入人工待
      审核队列，而非直接自动入库/直接丢弃"的完整实现。

    这条路径写入的边一律标记 provenance=AUTO_MERGED（见
    app/graphrag/provenance.py）——两侧实体精确对齐了术语表，不代表 LLM
    抽取出的这条关系本身就是对的，只是它没有进人工审核；调用方传入的
    now 统一作为这批关系的写入时间（不在循环内部逐条调用 datetime.now()，
    避免同一批次内的边打上有细微先后差异的时间戳，也方便测试用固定时钟）。
    """
    written = 0
    for relation in relations:
        subject_std = resolve_to_standard_name(relation["subject"], terms)
        object_std = resolve_to_standard_name(relation["object"], terms)
        if subject_std is None or object_std is None:
            suggested_subject = (
                None
                if subject_std is not None
                else find_fuzzy_candidate_standard_name(relation["subject"], terms)
            )
            suggested_object = (
                None
                if object_std is not None
                else find_fuzzy_candidate_standard_name(relation["object"], terms)
            )
            if suggested_subject is not None or suggested_object is not None:
                logger.info(
                    "关系候选模糊匹配到建议标准名，转人工审核 subject=%s "
                    "(建议=%s) object=%s (建议=%s)",
                    relation["subject"],
                    suggested_subject,
                    relation["object"],
                    suggested_object,
                )
                if review_conn is not None:
                    await enqueue_for_review(
                        review_conn,
                        subject_candidate=relation["subject"],
                        object_candidate=relation["object"],
                        relation_type=relation["relation_type"],
                        reason="fuzzy_match_needs_confirmation",
                        source=source,
                        tenant_id=tenant_id,
                        suggested_subject_standard_name=suggested_subject,
                        suggested_object_standard_name=suggested_object,
                        evidence=relation.get("evidence", ""),
                    )
                continue
            logger.info(
                "关系候选未能对齐术语表，丢弃 subject=%s object=%s",
                relation["subject"],
                relation["object"],
            )
            if review_conn is not None:
                reason = "subject_unresolved" if subject_std is None else "object_unresolved"
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason=reason,
                    source=source,
                    tenant_id=tenant_id,
                    evidence=relation.get("evidence", ""),
                )
            continue
        try:
            await graph_client.merge_relation(
                subject_standard_name=subject_std,
                object_standard_name=object_std,
                relation_type=relation["relation_type"],
                source=source,
                tenant_id=tenant_id,
                provenance=AUTO_MERGED,
                recorded_at=now,
            )
        except ValueError:
            logger.warning(
                "关系类型不合法，丢弃该候选 relation_type=%s",
                relation["relation_type"],
            )
            if review_conn is not None:
                await enqueue_for_review(
                    review_conn,
                    subject_candidate=relation["subject"],
                    object_candidate=relation["object"],
                    relation_type=relation["relation_type"],
                    reason="invalid_relation_type",
                    source=source,
                    tenant_id=tenant_id,
                    evidence=relation.get("evidence", ""),
                )
            continue
        written += 1
    return written
