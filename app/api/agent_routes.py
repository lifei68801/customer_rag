from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncIterator

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
from app.providers.tts import TTSProvider, TTSRequest
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

    文字提问（voice_response=False）且 LLM provider 支持 stream_complete()
    时，Responder 按句子边界逐句推送 `{"type": "delta", "text": ...}` 事件
    （复用 app/voice/streaming_responder.py 的切句逻辑，每句先过一次轻量
    规则安全检查——见 build_agent_graph 的 on_answer_chunk 参数说明）。

    语音请求（voice_response=True）且配置了 tts_provider 时，同样的流式
    生成驱动逐句合成：每句一从 Responder 产出（已经过轻量安全检查）就立刻
    调用 tts_provider 合成，推送 `{"type": "audio", "audio_base64": ...}`
    事件——这是 docs/ARCHITECTURE.md §7.3"首包延迟=等第一句生成完+该句
    合成耗时"而不是"等完整回复生成完"的落地，不再是"图跑完再对完整文本
    批量合成"。provider 不支持流式（没有 stream_complete）时自动退化为
    图跑完后对 final_text 做一次批量句子级合成（voice_output.py 原有
    行为），不强行流式。

    两种场景图执行完毕后都总是发送一个 `{"type": "final", ...}` 事件，
    字段和之前完全一致，是权威的最终结果——流式阶段累积的音频/文本片段
    也会汇总进这个事件里，方便只关心最终结果、不处理增量事件的简单
    客户端。如果完整语义安全审查事后判定不安全，final 事件里的 text/
    audio 会是兜底话术/兜底音频，可能与之前推送的增量内容不一致——已经
    推送的部分收不回来，这是流式输出的已知代价，已与产品方确认。

    Agent 自主规划（见 docs/AGENT_PLANNER_DESIGN.md）由
    settings.agent_enable_autonomous_planning 总控，但语音请求
    （voice_response=True）无论这个开关怎么配置都强制走确定性路径——
    Planner 多轮 LLM 往返和语音首包延迟的硬性要求直接冲突，不能让
    Planner 自己判断该不该省时间。Planner 路径的最终答案本身也不逐句
    流式推送/合成（只有静态 Responder 路径会调用 on_answer_chunk），
    和文字流式生成同一个既定范围。
    """
    enable_autonomous_planning = (
        settings.agent_enable_autonomous_planning and not payload.voice_response
    )

    async def event_stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        accumulated_audio_base64: list[str] = []

        async def on_text_chunk(sentence: str) -> None:
            body = json.dumps({"type": "delta", "text": sentence}, ensure_ascii=False)
            await queue.put(body)

        async def on_audio_chunk(sentence: str) -> None:
            # sentence 已经在 graph.py 的 responder_node 里过了一次分句轻量
            # 安全检查（命中风险词会被替换成安全兜底话术），这里直接合成，
            # 不重复检查。
            result = await tts_provider.synthesize(TTSRequest(text=sentence))
            encoded = base64.b64encode(result.audio_bytes).decode("ascii")
            accumulated_audio_base64.append(encoded)
            body = json.dumps(
                {"type": "audio", "audio_base64": encoded}, ensure_ascii=False
            )
            await queue.put(body)

        if payload.voice_response:
            on_answer_chunk = on_audio_chunk if tts_provider is not None else None
        else:
            on_answer_chunk = on_text_chunk

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
            on_answer_chunk=on_answer_chunk,
        )

        async def run_graph() -> dict[str, Any]:
            try:
                return await graph.ainvoke(
                    {
                        "question": payload.question,
                        "tenant_id": payload.tenant_id,
                        "session_id": payload.session_id,
                        "user_id": payload.user_id,
                    },
                    # LangGraph 默认 recursion_limit=25；Planner<->ToolCall 循环每轮
                    # 占 2 个节点步骤，留足余量防止状态内轮次计数器万一有 bug 时
                    # 直接被 LangGraph 自身的保护机制拦住，而不是无限循环耗尽资源。
                    config={
                        "recursion_limit": settings.agent_max_tool_call_rounds * 2 + 20
                    },
                )
            finally:
                await queue.put(None)

        graph_task = asyncio.create_task(run_graph())

        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {item}\n\n"

        result = await graph_task
        final_text = result.get("final_text", "")

        audio_segments_base64: list[str] | None = None
        if payload.voice_response and tts_provider is not None:
            if accumulated_audio_base64:
                # 流式阶段已经边生成边合成过，不重复合成，直接汇总
                audio_segments_base64 = accumulated_audio_base64
            else:
                # provider 不支持 stream_complete()，走原有批量合成兜底
                segments = await synthesize_voice_response(
                    final_text, tts_provider=tts_provider
                )
                audio_segments_base64 = [
                    base64.b64encode(segment).decode("ascii") for segment in segments
                ]

        body = json.dumps(
            {
                "type": "final",
                "text": final_text,
                "used_sources": result.get("used_sources", []),
                "audio_segments_base64": audio_segments_base64,
            },
            ensure_ascii=False,
        )
        yield f"data: {body}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
