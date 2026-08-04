from __future__ import annotations

from dataclasses import dataclass

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import VectorStore

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"


@dataclass(frozen=True)
class AnswerResult:
    text: str
    used_sources: list[str]


async def answer_question(
    question: str,
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    top_k: int = 3,
) -> AnswerResult:
    embed_result = await embedding_registry.run(
        EmbeddingRequest(texts=[question]),
        provider_name=embedding_provider_name,
    )
    query_vector = embed_result.vectors[0]

    records = await vector_store.search(query_vector, top_k=top_k)
    context = "\n\n".join(record.text for record in records)

    prompt = _PROMPT_TEMPLATE.format(context=context, question=question)
    llm_result = await llm_registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=[{"role": "user", "content": prompt}]),
        provider_name=llm_provider_name,
    )

    return AnswerResult(
        text=llm_result.text,
        used_sources=[record.id for record in records],
    )
