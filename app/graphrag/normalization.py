from __future__ import annotations

import logging
from typing import Any, Protocol

from app.graphrag.ontology import Term

logger = logging.getLogger(__name__)


class GraphWriteClientProtocol(Protocol):
    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
    ) -> None: ...


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
) -> int:
    """候选关系归一化对齐术语表后写入图谱，返回成功写入数。

    任一侧未能对齐标准术语的候选直接丢弃，不自动入库——这是架构文档
    "低置信度新实体进入人工待审核队列，而非直接自动入库"原则的最小
    实现：本阶段暂不接入真正的人工审核队列，只做"丢弃"这一半，避免
    引入尚无使用方的队列基础设施。
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
            continue
        try:
            await graph_client.merge_relation(
                subject_standard_name=subject_std,
                object_standard_name=object_std,
                relation_type=relation["relation_type"],
            )
        except ValueError:
            logger.warning(
                "关系类型不合法，丢弃该候选 relation_type=%s",
                relation["relation_type"],
            )
            continue
        written += 1
    return written
