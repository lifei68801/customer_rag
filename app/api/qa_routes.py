from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.config.settings import Settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.terms_store import list_terms
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.qa.answer import answer_question
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorStore

router = APIRouter()


class QARequest(BaseModel):
    question: str
    # tenant_id 优先从网关注入的 X-Tenant-Id 头读取（见
    # deps.get_gateway_tenant_id），这里保留为可选字段仅作为网关未配置
    # 时的本地开发兜底，见
    # docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md。
    tenant_id: str | None = None


class QAResponse(BaseModel):
    text: str
    used_sources: list[str]


@router.post("/qa", response_model=QAResponse)
async def qa_endpoint(
    payload: QARequest,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    settings: Settings = Depends(deps.get_settings),
) -> QAResponse:
    tenant_id = deps.resolve_tenant_id(
        gateway_tenant_id, payload.tenant_id, source="qa"
    )
    # 直接用上面刚解析出的权威 tenant_id 查术语表，不经过 deps.get_terms
    # 那套独立解析 tenant_id 的 Depends，见 app/api/deps.py 顶部说明。
    terms = await list_terms(review_conn, tenant_id)
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
        tenant_id=tenant_id,
        banned_terms=deps.parse_banned_terms(settings.banned_terms),
    )
    return QAResponse(text=result.text, used_sources=result.used_sources)
