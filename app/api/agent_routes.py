from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.agent.graph import build_agent_graph
from app.api import deps
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorStore

router = APIRouter()


class AgentChatRequest(BaseModel):
    question: str


@router.post("/agent/chat")
async def agent_chat_endpoint(
    payload: AgentChatRequest,
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient | None = Depends(deps.get_graph_client),
    terms: list[Term] = Depends(deps.get_terms),
) -> StreamingResponse:
    """Agent 推理入口，SSE 传输。

    当前只在图执行完毕后发送一个完整结果事件，尚不支持逐 token 增量
    输出——真正的流式生成需要 provider 层支持 streaming completion
    （解析 SSE 分片），目前 OpenAICompatibleChatProvider 只有一次性
    complete()，这部分尚未实现。此处先把 SSE 传输协议本身接通，
    后续给 provider 层加流式支持后可以平滑升级为逐 token 推送。
    """
    graph = build_agent_graph(
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

    async def event_stream() -> AsyncIterator[str]:
        result = await graph.ainvoke({"question": payload.question})
        body = json.dumps(
            {
                "text": result.get("final_text", ""),
                "used_sources": result.get("used_sources", []),
            },
            ensure_ascii=False,
        )
        yield f"data: {body}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
