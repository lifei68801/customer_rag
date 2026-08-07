from __future__ import annotations

import asyncio
import dataclasses

from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider, RerankRequest
from app.qa.query_rewrite import rewrite_query
from app.retrieval.bm25 import BM25Index
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.vector_store import VectorRecord, VectorStore


async def hybrid_search(
    question: str,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    tenant_id: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    query_rewrite_timeout_sec: float = 1.0,
    conversation_context: list[dict[str, str]] | None = None,
    vector_top_k: int = 10,
    bm25_top_k: int = 10,
    fusion_top_k: int = 10,
    final_top_k: int = 3,
) -> list[VectorRecord]:
    """原始query向量检索 + 改写query向量检索 + BM25 三路 -> RRF融合 -> 可选Rerank。

    RRF 只依据各路排名位置融合，不比较向量相似度和 BM25 分数这两种不同量纲；
    Rerank 仅用于对融合后的候选池精排，不改变候选池本身，但会把每条记录的
    score 覆盖为 rerank 返回的 relevance_score——下游 Fallback 判断（见
    app/agent/graph.py::route_after_retrieval）依据的置信度分数因此来自
    精排结果而不是向量检索阶段的原始相似度。未配置 rerank_provider 时
    score 保持融合前的原始值不变。

    conversation_context 为可选项：传入近期对话轮次时，query 改写这一步
    能看到"用户之前说了什么"来补全模糊指代（"这个报错"），见
    app/qa/query_rewrite.py；不传则只看孤立的当前问题，行为不变。

    query_texts（原始问题 + 可能的改写问题）各自的向量检索用 asyncio.gather
    并发执行，而不是顺序等待——两条链路各自都是一次 embedding API 调用加一次
    Milvus 查询，顺序执行会让总延迟翻倍。BM25 检索是同步内存操作，没有 IO
    等待，不需要纳入并发调度。
    """
    candidates: dict[str, VectorRecord] = {}
    ranked_id_lists: list[list[str]] = []

    query_texts = [question]
    if query_rewrite_enabled:
        rewritten = await rewrite_query(
            question,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            timeout_sec=query_rewrite_timeout_sec,
            conversation_context=conversation_context,
        )
        if rewritten != question:
            query_texts.append(rewritten)

    async def _vector_search_for_text(text: str) -> list[VectorRecord]:
        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=[text]),
            provider_name=embedding_provider_name,
        )
        return await vector_store.search(
            embed_result.vectors[0], top_k=vector_top_k, tenant_id=tenant_id
        )

    per_text_hits = await asyncio.gather(
        *(_vector_search_for_text(text) for text in query_texts)
    )
    for vector_hits in per_text_hits:
        ranked_id_lists.append([record.id for record in vector_hits])
        for record in vector_hits:
            candidates[record.id] = record

    bm25_hits = bm25_index.search(question, top_k=bm25_top_k, tenant_id=tenant_id)
    ranked_id_lists.append([hit.id for hit in bm25_hits])
    for hit in bm25_hits:
        candidates.setdefault(
            hit.id,
            VectorRecord(
                id=hit.id, vector=[], text=hit.text, tenant_id=tenant_id, metadata={}
            ),
        )

    fused = reciprocal_rank_fusion(*ranked_id_lists)
    fused_ids = [doc_id for doc_id, _ in fused][:fusion_top_k]
    fused_records = [candidates[doc_id] for doc_id in fused_ids if doc_id in candidates]

    if rerank_provider is None or not fused_records:
        return fused_records[:final_top_k]

    rerank_result = await rerank_provider.rerank(
        RerankRequest(
            query=question,
            documents=[record.text for record in fused_records],
            top_n=final_top_k,
        )
    )
    return [
        dataclasses.replace(fused_records[hit.index], score=hit.relevance_score)
        for hit in rerank_result.hits[:final_top_k]
    ]
