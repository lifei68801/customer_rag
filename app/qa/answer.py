from __future__ import annotations

from dataclasses import dataclass

from app.graphrag.ontology import Term
from app.graphrag.term_guard import GraphClientProtocol, build_term_guard_context
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorStore

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"


@dataclass(frozen=True)
class AnswerResult:
    text: str
    used_sources: list[str]
    retrieved_context: str


async def answer_question(
    question: str,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: GraphClientProtocol | None = None,
    top_k: int = 3,
) -> AnswerResult:
    term_guard_context: str | None = None
    if terms and graph_client is not None:
        term_guard_context = await build_term_guard_context(
            question, terms=terms, graph_client=graph_client
        )

    records = await hybrid_search(
        question,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        rerank_provider=rerank_provider,
        query_rewrite_enabled=query_rewrite_enabled,
        final_top_k=top_k,
    )
    retrieved_context = "\n\n".join(record.text for record in records)
    prompt_context = retrieved_context
    if term_guard_context:
        prompt_context = f"{term_guard_context}\n\n{retrieved_context}"

    prompt = _PROMPT_TEMPLATE.format(context=prompt_context, question=question)
    llm_result = await llm_registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=[{"role": "user", "content": prompt}]),
        provider_name=llm_provider_name,
    )

    return AnswerResult(
        text=llm_result.text,
        used_sources=[record.id for record in records],
        retrieved_context=retrieved_context,
    )
