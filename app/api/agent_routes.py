from __future__ import annotations

import base64
import json
from typing import AsyncIterator

import aiosqlite
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.agent.graph import build_agent_graph
from app.api import deps
from app.config.settings import Settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.providers.tts import TTSProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorStore
from app.voice.voice_output import synthesize_voice_response

router = APIRouter()


class AgentChatRequest(BaseModel):
    question: str
    # 里程碑X：多租户隔离已接入。tenant_id 目前直接来自请求体，
    # 真正上生产前需要换成从认证层（网关/JWT）注入，而不是信任客户端自报。
    tenant_id: str
    session_id: str = "default"
    user_id: str = "anonymous"
    # 按需触发：仅当本轮以语音提问时才为 true，文字提问始终为 false，
    # 避免不必要的 TTS 成本和延迟。
    voice_response: bool = False


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
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
    tts_provider: TTSProvider | None = Depends(deps.get_tts_provider),
    settings: Settings = Depends(deps.get_settings),
) -> StreamingResponse:
    """Agent 推理入口，SSE 传输。

    当前只在图执行完毕后发送一个完整结果事件，尚不支持逐 token 增量
    输出——真正的流式生成需要 provider 层支持 streaming completion
    （解析 SSE 分片），目前 OpenAICompatibleChatProvider 只有一次性
    complete()，这部分尚未实现。此处先把 SSE 传输协议本身接通，
    后续给 provider 层加流式支持后可以平滑升级为逐 token 推送。

    语音输出同理是简化版：句子级合成本身有做（voice_output.py），但
    没有和 Responder 的 token 流式生成过程流水线化（因为上面那条也没
    做），所以"首包延迟"目前等于"完整回答生成完+全部句子合成完"，
    不是架构文档 7.3 节设想的"边生成边合成"。

    Agent 自主规划（见 docs/AGENT_PLANNER_DESIGN.md）由
    settings.agent_enable_autonomous_planning 总控，但语音请求
    （voice_response=True）无论这个开关怎么配置都强制走确定性路径——
    Planner 多轮 LLM 往返和语音首包延迟的硬性要求直接冲突，不能让
    Planner 自己判断该不该省时间。
    """
    enable_autonomous_planning = (
        settings.agent_enable_autonomous_planning and not payload.voice_response
    )
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
        memory_conn=memory_conn,
        ticket_conn=memory_conn,
        min_relevance_score=settings.agent_min_relevance_score,
        enable_autonomous_planning=enable_autonomous_planning,
        max_tool_call_rounds=settings.agent_max_tool_call_rounds,
    )

    async def event_stream() -> AsyncIterator[str]:
        result = await graph.ainvoke(
            {
                "question": payload.question,
                "tenant_id": payload.tenant_id,
                "session_id": payload.session_id,
                "user_id": payload.user_id,
            },
            # LangGraph 默认 recursion_limit=25；Planner<->ToolCall 循环每轮
            # 占 2 个节点步骤，留足余量防止状态内轮次计数器万一有 bug 时
            # 直接被 LangGraph 自身的保护机制拦住，而不是无限循环耗尽资源。
            config={"recursion_limit": settings.agent_max_tool_call_rounds * 2 + 20},
        )
        final_text = result.get("final_text", "")

        audio_segments_base64: list[str] | None = None
        if payload.voice_response and tts_provider is not None:
            segments = await synthesize_voice_response(
                final_text, tts_provider=tts_provider
            )
            audio_segments_base64 = [
                base64.b64encode(segment).decode("ascii") for segment in segments
            ]

        body = json.dumps(
            {
                "text": final_text,
                "used_sources": result.get("used_sources", []),
                "audio_segments_base64": audio_segments_base64,
            },
            ensure_ascii=False,
        )
        yield f"data: {body}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
