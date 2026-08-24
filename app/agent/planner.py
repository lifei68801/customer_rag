from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Awaitable, Callable

from app.agent.tools import (
    GRAPH_QUERY_TOOL_SCHEMA,
    STRUCTURED_FILTER_QUERY_TOOL_SCHEMA,
    VECTOR_SEARCH_TOOL_SCHEMA,
    graph_query_tool,
    structured_filter_query_tool,
    vector_search_tool,
)
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.term_guard import GraphClientProtocol, describe_association
from app.providers.base import ProviderCapability, ProviderRequest, ProviderStreamChunk, ToolCall
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorRecord, VectorStore
from app.safety.rules import LITE_SAFETY_FALLBACK_SENTENCE, check_text
from app.voice.streaming_responder import stream_sentences

_TOOL_SCHEMAS = [VECTOR_SEARCH_TOOL_SCHEMA, GRAPH_QUERY_TOOL_SCHEMA, STRUCTURED_FILTER_QUERY_TOOL_SCHEMA]


def _build_tool_call_round_result(
    messages: list[dict[str, Any]],
    answer_text: str,
    tool_calls: list[ToolCall],
    *,
    round_num: int,
    max_tool_call_rounds: int,
) -> dict[str, Any]:
    """构造"这一轮模型请求了工具调用"场景下的返回值：轮次超限就放弃
    （不追加消息、不执行工具，转 Fallback）；没超限就把 assistant 消息
    （带 tool_calls 字段）追加进对话历史，返回待执行的工具调用列表。
    run_planner_turn（非流式）和 run_planner_turn_streaming（流式）在这
    一步的逻辑完全一样，抽成这个共用函数，避免两处重复维护。
    """
    if round_num >= max_tool_call_rounds:
        return {"planner_gave_up": True}
    messages = [
        *messages,
        {
            "role": "assistant",
            "content": answer_text or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ],
        },
    ]
    return {
        "planner_messages": messages,
        "pending_tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
        ],
    }


async def run_planner_turn(
    state: dict[str, Any],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    max_tool_call_rounds: int,
) -> dict[str, Any]:
    """执行一轮 Planner 推理：调用 LLM，决定"再调工具"还是"给出最终答案"。

    round_num 语义是"已经完成的工具调用轮次"；只有当 LLM 在 round_num 已经
    达到上限时仍要求调用工具，才强制放弃（planner_gave_up=True），转 Fallback——
    绝不在放弃后仍然执行它请求的工具，那样等于绕过了轮次上限。
    """
    messages = list(state.get("planner_messages", []))
    round_num = state.get("tool_call_round", 0)

    result = await llm_registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=messages, tools=_TOOL_SCHEMAS, tool_choice="auto"),
        provider_name=llm_provider_name,
    )

    if result.tool_calls:
        return _build_tool_call_round_result(
            messages, result.text, result.tool_calls,
            round_num=round_num, max_tool_call_rounds=max_tool_call_rounds,
        )

    messages.append({"role": "assistant", "content": result.text})
    return {
        "planner_messages": messages,
        "answer_text": result.text,
        "planner_gave_up": False,
    }


async def _split_stream_text_and_tool_calls(
    raw_stream: AsyncIterator[ProviderStreamChunk],
    tool_calls_box: list[list[ToolCall] | None],
) -> AsyncIterator[str]:
    """把 provider 流拆成两路：文本增量原样 yield 出去供 stream_sentences()
    消费；工具调用（如果有）写进 tool_calls_box[0]，供调用方在这个生成器
    耗尽后读取——用长度为 1 的列表当"可写引用"，闭包不能直接对外层局部
    变量重新赋值。
    """
    async for chunk in raw_stream:
        if chunk.text:
            yield chunk.text
        if chunk.tool_calls is not None:
            tool_calls_box[0] = chunk.tool_calls


async def run_planner_turn_streaming(
    state: dict[str, Any],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    max_tool_call_rounds: int,
    banned_terms: list[str] | None,
    on_answer_chunk: Callable[[str], Awaitable[None]],
    on_tool_status: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """run_planner_turn 的流式版本：语义完全一致（同样的轮次上限检查、
    同样的 planner_messages 追加规则），区别只是这一轮的文本用
    stream_complete_with_tools() 边生成边推送，而不是一次性拿到完整
    文本。每句文本先过 check_text 轻量规则检查（跟确定性路径的
    responder_node 完全一致），命中就换成 LITE_SAFETY_FALLBACK_SENTENCE
    再推送。见 docs/superpowers/specs/2026-08-23-
    planner-streaming-typewriter-design.md。

    on_tool_status 只在确认这一轮真的会继续执行工具调用（没有触发
    planner_gave_up）时才调用一次——轮次耗尽直接放弃的场景不应该让
    用户以为"还在查"。
    """
    messages = list(state.get("planner_messages", []))
    round_num = state.get("tool_call_round", 0)

    raw_stream = llm_registry.stream_with_tools(
        ProviderCapability.LLM,
        ProviderRequest(messages=messages, tools=_TOOL_SCHEMAS, tool_choice="auto"),
        provider_name=llm_provider_name,
    )
    tool_calls_box: list[list[ToolCall] | None] = [None]
    text_stream = _split_stream_text_and_tool_calls(raw_stream, tool_calls_box)

    sent_sentences: list[str] = []
    async for sentence in stream_sentences(text_stream):
        safety_result = check_text(sentence, banned_terms=banned_terms, include_email=False)
        safe_sentence = sentence if safety_result.is_safe else LITE_SAFETY_FALLBACK_SENTENCE
        await on_answer_chunk(safe_sentence)
        sent_sentences.append(safe_sentence)
    full_text = "".join(sent_sentences)

    tool_calls = tool_calls_box[0]
    if tool_calls:
        result = _build_tool_call_round_result(
            messages, full_text, tool_calls,
            round_num=round_num, max_tool_call_rounds=max_tool_call_rounds,
        )
        if not result.get("planner_gave_up"):
            await on_tool_status()
        return result

    messages = [*messages, {"role": "assistant", "content": full_text}]
    return {
        "planner_messages": messages,
        "answer_text": full_text,
        "planner_gave_up": False,
    }


async def _dispatch_tool_call(
    name: str,
    arguments: dict[str, Any],
    *,
    tenant_id: str,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None,
    query_rewrite_enabled: bool,
    terms: list[Term] | None,
    graph_client: GraphClientProtocol | None,
    confirmed_relation_types: set[str] | None,
    term_type_schema: dict[str, TermTypeCategory] | None,
) -> tuple[str, list[VectorRecord]]:
    """执行单个工具调用，返回 (供 LLM 看的观察结果 JSON 字符串, 新增的检索记录)。

    tenant_id 永远用调用方传入的值（来自 AgentState），完全忽略 arguments 里
    可能出现的同名字段——不管 LLM 输出了什么，这里都不采信。
    """
    if name == "vector_search_tool":
        query = str(arguments.get("query", ""))
        records = await vector_search_tool(
            query,
            tenant_id=tenant_id,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
        )
        observation = {
            "results": [{"id": r.id, "text": r.text} for r in records]
        }
        return json.dumps(observation, ensure_ascii=False), records

    if name == "graph_query_tool":
        if not (terms and graph_client is not None):
            return json.dumps({"error": "graph_query_tool 未配置"}, ensure_ascii=False), []
        entity_name = str(arguments.get("entity_name", ""))
        entity_type = arguments.get("entity_type") or None
        result = await graph_query_tool(
            entity_name, terms=terms, tenant_id=tenant_id, graph_client=graph_client,
            entity_type=entity_type,
        )
        observation = {
            "resolved": result.resolved,
            "standard_name": result.standard_name,
            "subgraph": [
                {**row, "association": describe_association(row.get("hops", 1))}
                for row in result.subgraph
            ],
        }
        return json.dumps(observation, ensure_ascii=False), []

    if name == "structured_filter_query_tool":
        if graph_client is None or confirmed_relation_types is None or term_type_schema is None:
            return json.dumps({"error": "structured_filter_query_tool 未配置"}, ensure_ascii=False), []
        observation = await structured_filter_query_tool(
            arguments, tenant_id=tenant_id, graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types, term_type_schema=term_type_schema,
        )
        return json.dumps(observation, ensure_ascii=False), []

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False), []


async def run_tool_calls(
    state: dict[str, Any],
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
    confirmed_relation_types: set[str] | None = None,
    term_type_schema: dict[str, TermTypeCategory] | None = None,
) -> dict[str, Any]:
    """执行 state["pending_tool_calls"] 里的每一个工具调用，结果回填对话历史。

    每个工具的执行结果都会被追加为一条 role="tool" 消息（OpenAI 协议要求的
    格式），供下一轮 Planner 推理时看到；解析 arguments 失败时不抛异常，
    回填一条 {"error": ...} 观察结果，让 Planner 有机会自行调整重试。
    """
    tenant_id = state["tenant_id"]
    messages = list(state.get("planner_messages", []))
    retrieved_records = list(state.get("retrieved_records", []))
    tool_results = list(state.get("tool_results", []))
    pending_calls = state.get("pending_tool_calls", [])

    async def _execute_one(call: dict[str, Any]) -> tuple[dict, list[VectorRecord]]:
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            content = json.dumps({"error": "arguments 不是合法 JSON"}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        content, new_records = await _dispatch_tool_call(
            call["name"],
            arguments,
            tenant_id=tenant_id,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            confirmed_relation_types=confirmed_relation_types,
            term_type_schema=term_type_schema,
        )
        return (
            {"tool_call_id": call["id"], "name": call["name"], "content": content},
            new_records,
        )

    # 同一轮 LLM 可能同时请求多个工具（比如 vector_search_tool +
    # graph_query_tool），彼此没有数据依赖——2026-08-10 起改成
    # asyncio.gather 并发执行，不再是 for 循环顺序 await。结果顺序按
    # pending_calls 原始顺序组装（asyncio.gather 保证返回顺序和传入协程
    # 顺序一致，不按完成先后），不因为改成并发就打乱 tool_call_id 对应
    # 关系的可读性。
    # 用 return_exceptions=True 等所有工具调用都跑完（不管成败）再决定要不要
    # 重新抛出，而不是用 gather 默认行为——默认行为下一个工具调用失败会让
    # gather 立刻返回，其它还在执行的工具调用变成没人处理的后台任务，可能
    # 引发不该发生的副作用，也可能在稍后失败时报一个不会被任何人处理的
    # "未获取异常"。这里等两边都落地再决定要不要抛，语义上仍然和串行版本
    # 一致：任一工具调用失败都让整个 run_tool_calls() 失败。
    outcomes = await asyncio.gather(
        *(_execute_one(call) for call in pending_calls), return_exceptions=True
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome

    for tool_result, new_records in outcomes:
        existing_ids = {r.id for r in retrieved_records}
        retrieved_records.extend(r for r in new_records if r.id not in existing_ids)
        tool_results.append(tool_result)
        messages.append({"role": "tool", "tool_call_id": tool_result["tool_call_id"], "content": tool_result["content"]})

    return {
        "planner_messages": messages,
        "pending_tool_calls": [],
        "retrieved_records": retrieved_records,
        "used_sources": [r.id for r in retrieved_records],
        "tool_results": tool_results,
        "tool_call_round": state.get("tool_call_round", 0) + 1,
    }


def route_after_planner(state: dict[str, Any]) -> str:
    """Planner 之后走 tool_call / responder / fallback 三选一。"""
    if state.get("planner_gave_up"):
        return "fallback"
    if state.get("pending_tool_calls"):
        return "tool_call"
    return "responder"
