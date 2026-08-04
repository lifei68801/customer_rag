from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.qa.answer import answer_question
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
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
) -> QAResponse:
    result = await answer_question(
        payload.question,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
        llm_registry=llm_registry,
        llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
    )
    return QAResponse(text=result.text, used_sources=result.used_sources)
