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
from app.graphrag.ontology_categories import list_term_types
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.terms_store import list_terms
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
    # tenant_id 优先从网关注入的 X-Tenant-Id 头读取（见
    # deps.get_gateway_tenant_id），这里保留为可选字段仅作为网关未配置
    # 时的本地开发兜底，见
    # docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md。
    tenant_id: str | None = None
    session_id: str = "default"
    user_id: str = "anonymous"
    # 按需触发：仅当本轮以语音提问时才为 true，文字提问始终为 false，
    # 避免不必要的 TTS 成本和延迟。
    voice_response: bool = False


@router.post("/agent/chat")
async def agent_chat_endpoint(
    payload: AgentChatRequest,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient | None = Depends(deps.get_graph_client),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
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
    Planner 自己判断该不该省时间。

    Planner 路径（enable_autonomous_planning=True 且非语音请求）现在
    也会流式输出：LLM provider 支持 stream_complete_with_tools() 时，
    每一轮推理产生的文本会跟静态路径一样按句子边界逐句推送
    `{"type": "delta", ...}` 事件；provider 不支持这个能力时透明退化
    为一次性拿到完整答案（Planner 原有行为不变）。多轮工具调用（查
    知识图谱/向量库等）期间会额外推送一次
    `{"type": "tool_status", "text": "正在查询相关信息..."}` 事件，
    给前端一个"正在查询"的状态反馈，不是最终答案的一部分。见
    docs/superpowers/specs/2026-08-23-planner-streaming-typewriter-design.md。
    """
    tenant_id = deps.resolve_tenant_id(
        gateway_tenant_id, payload.tenant_id, source="agent_chat"
    )
    # 直接用上面刚解析出的权威 tenant_id 查术语表/已确认 schema，不经过
    # deps.get_terms 等独立解析 tenant_id 的 Depends——网关未配置时后者会
    # 悄悄回退到硬编码的 "default" 租户，跟这里的 tenant_id 不是同一个值，
    # 见 app/api/deps.py 顶部关于这几个函数已删除的说明。
    terms = await list_terms(review_conn, tenant_id)
    confirmed_relation_types = {
        rt.relation_type
        for rt in await list_relation_types(review_conn, tenant_id, status="confirmed")
    }
    term_type_schema = {
        c.value: c for c in await list_term_types(review_conn, tenant_id, status="confirmed")
    }
    enable_autonomous_planning = (
        settings.agent_enable_autonomous_planning and not payload.voice_response
    )

    async def event_stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        accumulated_audio_base64: list[str] = []

        async def on_text_chunk(sentence: str) -> None:
            body = json.dumps({"type": "delta", "text": sentence}, ensure_ascii=False)
            await queue.put(body)

        async def on_tool_status() -> None:
            body = json.dumps(
                {"type": "tool_status", "text": "正在查询相关信息..."}, ensure_ascii=False
            )
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
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
            graph_client=graph_client,
            memory_conn=memory_conn,
            ticket_conn=memory_conn,
            memory_recall_use_embedding=settings.memory_recall_use_embedding,
            min_relevance_score=settings.agent_min_relevance_score,
            enable_autonomous_planning=enable_autonomous_planning,
            max_tool_call_rounds=settings.agent_max_tool_call_rounds,
            on_answer_chunk=on_answer_chunk,
            on_tool_status=on_tool_status if not payload.voice_response else None,
            banned_terms=deps.parse_banned_terms(settings.banned_terms),
        )

        async def run_graph() -> dict[str, Any]:
            try:
                return await graph.ainvoke(
                    {
                        "question": payload.question,
                        "tenant_id": tenant_id,
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
                    final_text,
                    tts_provider=tts_provider,
                    banned_terms=deps.parse_banned_terms(settings.banned_terms),
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
