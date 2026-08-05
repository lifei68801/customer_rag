from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.qa.answer import answer_question
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorStore

router = APIRouter()


class QARequest(BaseModel):
    question: str


class QAResponse(BaseModel):
    text: str
    used_sources: list[str]


@router.post("/qa", response_model=QAResponse)
async def qa_endpoint(
    payload: QARequest,
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    terms: list[Term] = Depends(deps.get_terms),
) -> QAResponse:
    result = await answer_question(
        payload.question,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        rerank_provider=rerank_provider,
        terms=terms,
        graph_client=graph_client,
    )
    return QAResponse(text=result.text, used_sources=result.used_sources)
