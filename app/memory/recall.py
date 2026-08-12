from __future__ import annotations

import math
from typing import Any

import aiosqlite

from app.memory.memory_store import list_active_memory_items
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.retrieval.bm25 import BM25Index
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.vector_store import VectorRecord


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _recency_weights(items: list[dict[str, Any]]) -> dict[str, float]:
    """items 已经按 updated_at 升序排列（list_active_memory_items 的返回顺序），
    用排位而非绝对时间差算衰减权重——不解析 SQLite 时间字符串/时区，只保证
    "越新权重越高"这一单调关系，即满足架构文档"近因衰减"的语义要求。"""
    n = len(items)
    return {item["memory_id"]: (i + 1) / n for i, item in enumerate(items)}


def _mmr_select(
    candidates: list[tuple[dict[str, Any], float]],
    *,
    top_k: int,
    lambda_: float,
) -> list[dict[str, Any]]:
    """标准 MMR：每步选"和已选结果里最相似的那条也不太像"、同时 relevance
    仍然高的候选，避免返回好几条高度雷同的记忆挤占上下文预算。

    候选缺 embedding 时，与已选项的相似度视为 0（既不因为"没有向量"被
    错误地当作高度相似而排除，也不假装能算出真实相似度）。
    """
    remaining = list(candidates)
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < top_k:
        best_index = 0
        best_score = None
        for index, (item, relevance) in enumerate(remaining):
            if selected:
                max_sim = max(
                    (
                        _cosine_similarity(item["embedding"], other["embedding"])
                        if item.get("embedding") is not None
                        and other.get("embedding") is not None
                        else 0.0
                    )
                    for other in selected
                )
            else:
                max_sim = 0.0
            score = lambda_ * relevance - (1 - lambda_) * max_sim
            if best_score is None or score > best_score:
                best_score = score
                best_index = index
        item, _ = remaining.pop(best_index)
        selected.append(item)
    return selected


async def recall_memory_items(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    question: str,
    embedding_registry: EmbeddingRegistry | None = None,
    embedding_provider_name: str | None = None,
    top_k: int = 5,
    mmr_lambda: float = 0.5,
    use_embedding: bool = True,
) -> list[dict[str, Any]]:
    """多源召回融合 + MMR 去重，取代"返回全部 active 记忆条目按更新时间截断"。

    覆盖架构文档 §6.3 四路里的三路：P2 语义向量检索、P3 长期记忆条目检索
    （全集候选池）、P4 BM25 关键词检索；P1"结构化历史检索（按客户ID+时间
    窗口）"未实现——依赖尚未构建的时间/指代解析层，这是明确的范围缩减，
    不是疏漏。

    融合方式：语义排名 + BM25 排名先做 RRF 融合（不同量纲的分数不能直接
    相加比较，这一点和检索层的混合检索是同一个原则），RRF 分数再乘以记忆
    置信度和近因排位权重得到最终 relevance，作为 MMR 选择时的相关性依据。

    已知迁移缺口：没有 embedding 的历史记忆条目（这次改动上线前写入的，
    或从未走 apply_memory_actions embedding 路径写入的）在语义这一路上
    完全不参与排名，只能靠 BM25 关键词命中才可能被召回——如果关键词也
    不重合就会被漏掉，不再像旧版"全部返回"那样兜底。要解决需要对存量
    memory_items 跑一次 embedding 回填脚本，这里不做，仅记录风险。

    use_embedding=False 时完全跳过语义这一路（不发起 embedding API 调用，
    此时 embedding_registry/embedding_provider_name 可以不传），只用 BM25
    关键词排名——2026-08-12 排查响应延迟时加的临时降级开关（见
    Settings.memory_recall_use_embedding），单路排名直接当最终排名用，
    不需要跟自己做 RRF 融合。
    """
    items = await list_active_memory_items(conn, tenant_id=tenant_id, user_id=user_id)
    if not items:
        return []

    recency_weights = _recency_weights(items)

    bm25_index = BM25Index()
    bm25_records = [
        VectorRecord(
            id=item["memory_id"],
            vector=[],
            text=item["text"],
            tenant_id=tenant_id,
            metadata={},
        )
        for item in items
    ]
    bm25_index.index(bm25_records)
    bm25_hits = bm25_index.search(question, top_k=len(items), tenant_id=tenant_id)
    bm25_rank_ids = [hit.id for hit in bm25_hits]

    if use_embedding:
        assert embedding_registry is not None and embedding_provider_name is not None
        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=[question]), provider_name=embedding_provider_name
        )
        query_vector = embed_result.vectors[0]
        semantic_scored = [
            (item["memory_id"], _cosine_similarity(query_vector, item["embedding"]))
            for item in items
            if item.get("embedding") is not None
        ]
        semantic_scored.sort(key=lambda pair: pair[1], reverse=True)
        semantic_rank_ids = [memory_id for memory_id, _ in semantic_scored]
        fused = reciprocal_rank_fusion(semantic_rank_ids, bm25_rank_ids)
    else:
        fused = reciprocal_rank_fusion(bm25_rank_ids)

    items_by_id = {item["memory_id"]: item for item in items}
    candidates: list[tuple[dict[str, Any], float]] = []
    for memory_id, rrf_score in fused:
        item = items_by_id.get(memory_id)
        if item is None:
            continue
        confidence = item.get("confidence", 0.8)
        relevance = rrf_score * confidence * recency_weights.get(memory_id, 1.0)
        candidates.append((item, relevance))

    return _mmr_select(candidates, top_k=top_k, lambda_=mmr_lambda)
