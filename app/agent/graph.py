from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.create_ticket_tool import create_ticket
from app.agent.state import AgentState
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.term_guard import build_term_guard_context
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorStore
from app.safety.rules import check_text

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"
_UNSAFE_INPUT_MESSAGE = "您的问题包含无法处理的敏感内容，请修改后重新提问。"
_UNSAFE_OUTPUT_MESSAGE = "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"
_FALLBACK_MESSAGE = "抱歉，暂时没有找到确切答案，已为您转接人工客服处理。"


def build_agent_graph(
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
    graph_client: Neo4jGraphClient | None = None,
    banned_terms: list[str] | None = None,
    top_k: int = 3,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """构建 Agent 推理状态图：InputSafety -> TermGuard -> 检索 -> Responder/Fallback -> OutputSafety。

    范围说明（有意简化，非疏漏）：架构文档设想的 Planner 是"LLM 自主决策
    多轮调用 vector_search_tool/graph_query_tool"，这需要 provider 层支持
    真正的 function-calling（发送 tools schema、解析 tool_calls），目前
    provider 层尚未实现这一能力。本实现把 Planner 简化为确定性流程：
    TermGuard 强制注入 + 始终执行一次混合检索，再按"是否检索到结果"
    这一简单信号路由到 Responder 或 Fallback。真正的 LLM 驱动工具选择
    和多轮循环（含最大迭代次数保护）留待 provider 层具备 function-calling
    能力后再实现，不在此冒充。
    """

    async def input_safety_node(state: AgentState) -> dict[str, Any]:
        result = check_text(state["question"], banned_terms=banned_terms)
        return {
            "is_input_safe": result.is_safe,
            "input_unsafe_terms": result.matched_terms,
        }

    async def term_guard_node(state: AgentState) -> dict[str, Any]:
        if not (terms and graph_client is not None):
            return {"term_guard_context": None}
        context = await build_term_guard_context(
            state["question"], terms=terms, graph_client=graph_client
        )
        return {"term_guard_context": context}

    async def retrieval_node(state: AgentState) -> dict[str, Any]:
        records = await hybrid_search(
            state["question"],
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            final_top_k=top_k,
        )
        return {
            "retrieved_records": records,
            "used_sources": [record.id for record in records],
        }

    async def responder_node(state: AgentState) -> dict[str, Any]:
        context = "\n\n".join(
            record.text for record in state.get("retrieved_records", [])
        )
        term_guard_context = state.get("term_guard_context")
        if term_guard_context:
            context = f"{term_guard_context}\n\n{context}"
        prompt = _PROMPT_TEMPLATE.format(context=context, question=state["question"])
        result = await llm_registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=[{"role": "user", "content": prompt}]),
            provider_name=llm_provider_name,
        )
        return {"answer_text": result.text, "fallback_triggered": False}

    async def fallback_node(state: AgentState) -> dict[str, Any]:
        return {"answer_text": _FALLBACK_MESSAGE, "fallback_triggered": True}

    async def create_ticket_node(state: AgentState) -> dict[str, Any]:
        result = await create_ticket(
            question=state["question"], reason="检索结果不足，需人工介入"
        )
        return {"ticket_id": result.ticket_id}

    async def output_safety_node(state: AgentState) -> dict[str, Any]:
        if not state.get("is_input_safe", True):
            return {"is_output_safe": True, "final_text": _UNSAFE_INPUT_MESSAGE}
        answer = state.get("answer_text", "")
        result = check_text(answer, banned_terms=banned_terms)
        if not result.is_safe:
            return {"is_output_safe": False, "final_text": _UNSAFE_OUTPUT_MESSAGE}
        return {"is_output_safe": True, "final_text": answer}

    def route_after_input_safety(state: AgentState) -> str:
        return "term_guard" if state.get("is_input_safe", True) else "output_safety"

    def route_after_retrieval(state: AgentState) -> str:
        return "responder" if state.get("retrieved_records") else "fallback"

    graph: StateGraph[AgentState] = StateGraph(AgentState)
    graph.add_node("input_safety", input_safety_node)
    graph.add_node("term_guard", term_guard_node)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("responder", responder_node)
    graph.add_node("fallback", fallback_node)
    graph.add_node("create_ticket", create_ticket_node)
    graph.add_node("output_safety", output_safety_node)

    graph.add_edge(START, "input_safety")
    graph.add_conditional_edges(
        "input_safety",
        route_after_input_safety,
        {"term_guard": "term_guard", "output_safety": "output_safety"},
    )
    graph.add_edge("term_guard", "retrieval")
    graph.add_conditional_edges(
        "retrieval",
        route_after_retrieval,
        {"responder": "responder", "fallback": "fallback"},
    )
    graph.add_edge("responder", "output_safety")
    graph.add_edge("fallback", "create_ticket")
    graph.add_edge("create_ticket", "output_safety")
    graph.add_edge("output_safety", END)

    return graph.compile()
