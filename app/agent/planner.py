from __future__ import annotations

import json
from typing import Any

from app.agent.tools import (
    GRAPH_QUERY_TOOL_SCHEMA,
    VECTOR_SEARCH_TOOL_SCHEMA,
    graph_query_tool,
    vector_search_tool,
)
from app.graphrag.ontology import Term
from app.graphrag.term_guard import GraphClientProtocol
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorRecord, VectorStore

_TOOL_SCHEMAS = [VECTOR_SEARCH_TOOL_SCHEMA, GRAPH_QUERY_TOOL_SCHEMA]


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
        if round_num >= max_tool_call_rounds:
            return {"planner_gave_up": True}

        messages.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in result.tool_calls
                ],
            }
        )
        return {
            "planner_messages": messages,
            "pending_tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in result.tool_calls
            ],
        }

    messages.append({"role": "assistant", "content": result.text})
    return {
        "planner_messages": messages,
        "answer_text": result.text,
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
        result = await graph_query_tool(
            entity_name, terms=terms, tenant_id=tenant_id, graph_client=graph_client
        )
        observation = {
            "resolved": result.resolved,
            "standard_name": result.standard_name,
            "subgraph": result.subgraph,
        }
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

    for call in state.get("pending_tool_calls", []):
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            content = json.dumps({"error": "arguments 不是合法 JSON"}, ensure_ascii=False)
        else:
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
            )
            existing_ids = {r.id for r in retrieved_records}
            retrieved_records.extend(r for r in new_records if r.id not in existing_ids)

        tool_results.append({"tool_call_id": call["id"], "name": call["name"], "content": content})
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})

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
