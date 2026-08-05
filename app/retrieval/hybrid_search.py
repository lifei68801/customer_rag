from __future__ import annotations

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
    vector_top_k: int = 10,
    bm25_top_k: int = 10,
    fusion_top_k: int = 10,
    final_top_k: int = 3,
) -> list[VectorRecord]:
    """原始query向量检索 + 改写query向量检索 + BM25 三路 -> RRF融合 -> 可选Rerank。

    RRF 只依据各路排名位置融合，不比较向量相似度和 BM25 分数这两种不同量纲；
    Rerank 仅用于对融合后的候选池精排，不改变候选池本身。
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
        )
        if rewritten != question:
            query_texts.append(rewritten)

    for text in query_texts:
        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=[text]),
            provider_name=embedding_provider_name,
        )
        vector_hits = await vector_store.search(
            embed_result.vectors[0], top_k=vector_top_k, tenant_id=tenant_id
        )
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
    return [fused_records[hit.index] for hit in rerank_result.hits[:final_top_k]]
