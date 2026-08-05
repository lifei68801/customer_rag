from __future__ import annotations

import logging
from typing import Any, Protocol

import aiosqlite

from app.graphrag.ontology import Term
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
    ) -> None: ...

    async def delete_relations_by_source(self, source: str) -> None: ...


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


async def normalize_and_write_relations(
    relations: list[dict[str, str]],
    *,
    terms: list[Term],
    graph_client: GraphWriteClientProtocol,
    source: str,
    review_conn: aiosqlite.Connection | None = None,
) -> int:
    """候选关系归一化对齐术语表后写入图谱，返回成功写入数。

    任一侧未能对齐标准术语、或关系类型不合法的候选不会自动入库。
    review_conn 为可选项：
    - 不传（默认）：候选只记日志后丢弃，保持阶段3落地时的行为不变；
    - 传入：候选改为写入持久化的人工待审核队列（见 review_queue.py），
      而不是随日志一起消失——对应架构文档"低置信度新实体进入人工待
      审核队列，而非直接自动入库/直接丢弃"的完整实现。
    """
    written = 0
    for relation in relations:
        subject_std = resolve_to_standard_name(relation["subject"], terms)
        object_std = resolve_to_standard_name(relation["object"], terms)
        if subject_std is None or object_std is None:
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
                )
            continue
        try:
            await graph_client.merge_relation(
                subject_standard_name=subject_std,
                object_standard_name=object_std,
                relation_type=relation["relation_type"],
                source=source,
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
                )
            continue
        written += 1
    return written
