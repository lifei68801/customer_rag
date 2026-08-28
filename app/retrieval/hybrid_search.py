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

    conversation_context 为可选项，传给 query 改写这一步作为参考上下文。
    注意它【不再】负责补全模糊指代——2026-08-28 起指代消解统一由更上游的
    Layer 1（app/qa/query_rewrite.py::resolve_question，在 LangGraph 里是
    resolve_question_node）每轮做一次，rewrite_query 的提示词已收窄成只做
    "把口语化说法改写得更利于文档检索匹配"。不传则只看孤立的当前问题。

    query_texts（原始问题 + 可能的改写问题）各自的向量检索用 asyncio.gather
    并发执行。bm25 检索（同步、无 IO 等待，纯 CPU 计算）用 asyncio.to_thread
    包一层，和"改写+向量检索"整条链路并发发起——bm25_search 只依赖原始
    question，和改写结果没有数据依赖，2026-08-10 起不再排在改写+向量检索
    之后串行执行；包一层线程同时避免了 bm25 的同步计算独占事件循环。
    """
    candidates: dict[str, VectorRecord] = {}
    ranked_id_lists: list[list[str]] = []

    async def _vector_search_for_text(text: str) -> list[VectorRecord]:
        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=[text]),
            provider_name=embedding_provider_name,
        )
        return await vector_store.search(
            embed_result.vectors[0], top_k=vector_top_k, tenant_id=tenant_id
        )

    async def _rewrite_and_vector_search() -> list[list[VectorRecord]]:
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
        return await asyncio.gather(
            *(_vector_search_for_text(text) for text in query_texts)
        )

    async def _bm25_search() -> list:
        return await asyncio.to_thread(
            bm25_index.search, question, top_k=bm25_top_k, tenant_id=tenant_id
        )

    # bm25_search 只依赖原始 question，和"改写+向量检索"这条链路没有数据
    # 依赖，2026-08-10 起改成并发发起，不再排在改写+向量检索全部跑完之后；
    # 同时用 asyncio.to_thread 包一层——bm25 是纯同步 CPU 计算（每次对整个
    # 租户全量重算 tf/idf，没有预建倒排索引），不包线程会独占事件循环，
    # 期间同一进程收到的其它请求全部卡住排队，真实测试量过单次 3ms–308ms。
    # 用 return_exceptions=True 等两边都跑完再手动重新抛出，而不是用 gather
    # 默认行为：默认行为下一边抛异常会让 gather 立刻返回、另一边可能还在
    # 后台裸跑，产生不会被任何人处理的"未获取异常"。
    results = await asyncio.gather(
        _rewrite_and_vector_search(), _bm25_search(), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    per_text_hits, bm25_hits = results

    for vector_hits in per_text_hits:
        ranked_id_lists.append([record.id for record in vector_hits])
        for record in vector_hits:
            candidates[record.id] = record

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
