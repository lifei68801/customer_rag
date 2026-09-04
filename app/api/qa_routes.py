from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import deps
from app.config.settings import Settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.terms_store import list_terms_merged
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.qa.answer import answer_question
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorStore

# require_csrf 挂在 router 上而不是逐个路由：漏挂一个写接口不会有任何测试
# 变红，而漏掉的那一个就是活的 CSRF 通道。
#
# get_gateway_tenant_id 留在这里只当"网关凭证校验"用，不再参与租户解析：
# 身份改从会话取之后，配置了 gateway_shared_secret 的部署仍然必须带上有效的
# X-Gateway-Secret 才进得来（test_qa_routes.py 与 test_agent_chat_routes.py 里
# 的 rejects_wrong_gateway_secret 用例钉的就是这条路径还活着）。
router = APIRouter(
    dependencies=[Depends(deps.require_csrf), Depends(deps.get_gateway_tenant_id)]
)


class QARequest(BaseModel):
    question: str
    # 保留但不再使用：租户一律取自会话（deps.require_chat_session）。删掉
    # 这个字段会让还在发它的既有客户端直接 422，而忽略它是无声的兼容。
    tenant_id: str | None = None


class QAResponse(BaseModel):
    text: str
    used_sources: list[str]


@router.post("/qa", response_model=QAResponse)
async def qa_endpoint(
    payload: QARequest,
    identity: tuple[str, str] = Depends(deps.require_chat_session),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    settings: Settings = Depends(deps.get_settings),
) -> QAResponse:
    tenant_id, _user_id = identity
    # 直接用会话里的权威 tenant_id 查术语表，不经过 deps.get_terms
    # 那套独立解析 tenant_id 的 Depends，见 app/api/deps.py 顶部说明。
    terms = await list_terms_merged(review_conn, tenant_id)
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
