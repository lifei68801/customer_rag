from __future__ import annotations

from typing import Any

import aiosqlite
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.create_ticket_tool import create_ticket
from app.agent.planner import route_after_planner, run_planner_turn, run_tool_calls
from app.agent.state import AgentState
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.term_guard import build_term_guard_context
from app.memory.consolidation import run_memory_consolidation
from app.memory.context_injection import inject_memory_context
from app.memory.session_window import append_turn
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorStore
from app.safety.rules import check_text
from app.safety.semantic_review import semantic_safety_review

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"
_UNSAFE_INPUT_MESSAGE = "您的问题包含无法处理的敏感内容，请修改后重新提问。"
_UNSAFE_OUTPUT_MESSAGE = "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"
_FALLBACK_MESSAGE = "抱歉，暂时没有找到确切答案，已为您转接人工客服处理。"
_PLANNER_SYSTEM_PROMPT = (
    "你是客服问答助手。可以调用 vector_search_tool 检索知识库、"
    "graph_query_tool 查询专有名词的标准名称及关联关系。"
    "有足够信息时直接给出最终答案，不要编造资料中没有的内容；"
    "信息不足以回答时也不要编造。"
)


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
    memory_conn: aiosqlite.Connection | None = None,
    top_k: int = 3,
    enable_autonomous_planning: bool = False,
    max_tool_call_rounds: int = 3,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """构建 Agent 推理状态图。

    enable_autonomous_planning=False（默认）：InputSafety -> TermGuard ->
    MemoryRecall -> 检索 -> Responder/Fallback -> OutputSafety -> MemorySave。
    检索固定跑一次，按"是否检索到结果"这一确定性信号路由——这是阶段4
    落地时的简化实现，保留作为默认值，是新 Planner 路径出问题时的回退。

    enable_autonomous_planning=True：InputSafety -> TermGuard -> MemoryRecall
    -> Planner -> ToolCall（循环回 Planner，最多 max_tool_call_rounds 轮）
    -> Responder/Fallback -> OutputSafety -> MemorySave。LLM 自主决定调用
    vector_search_tool/graph_query_tool 还是直接回答，真正的 ReAct 风格
    多轮工具决策，对应架构文档 §3.2 的完整设计。详见
    docs/AGENT_PLANNER_DESIGN.md。

    两种模式下 TermGuard 强制注入都不受影响——TermGuard 是独立于 Planner
    的安全网，不因为换了推理路径就失效。

    memory_conn 为可选项：不传则完全跳过记忆召回/写入，图的行为与
    阶段4完全一致；传入则在 Responder 前注入长期记忆+近期会话上下文，
    并在 OutputSafety 后同步保存本轮对话+做一次记忆 consolidation。
    这里的"同步"是简化——架构文档设计的异步 consolidation 队列（不
    阻塞主响应）未实现，当前会让 consolidation 的 LLM 调用延迟计入
    本轮响应总耗时。
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

    async def memory_recall_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is None:
            return {"memory_context_messages": []}
        messages = await inject_memory_context(
            memory_conn,
            tenant_id=state["tenant_id"],
            session_id=state.get("session_id", ""),
            user_id=state.get("user_id", ""),
        )
        return {"memory_context_messages": messages}

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
            tenant_id=state["tenant_id"],
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
        messages = [
            *state.get("memory_context_messages", []),
            {"role": "user", "content": prompt},
        ]
        result = await llm_registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=messages),
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

        if state.get("fallback_triggered"):
            # 兜底话术是固定文案，不含 LLM 生成内容，跳过语义审查节省一次
            # 无意义的 LLM 调用。
            return {"is_output_safe": True, "final_text": answer}

        semantic_result = await semantic_safety_review(
            answer,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        if semantic_result.reviewed and not semantic_result.is_safe:
            return {
                "is_output_safe": False,
                "final_text": _UNSAFE_OUTPUT_MESSAGE,
                "semantic_review_reviewed": True,
            }
        return {
            "is_output_safe": True,
            "final_text": answer,
            "semantic_review_reviewed": semantic_result.reviewed,
        }

    async def memory_save_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is None:
            return {}
        session_id = state.get("session_id", "")
        user_id = state.get("user_id", "")
        final_text = state.get("final_text", "")
        await append_turn(
            memory_conn,
            tenant_id=state["tenant_id"],
            session_id=session_id,
            user_id=user_id,
            role="user",
            content=state["question"],
        )
        await append_turn(
            memory_conn,
            tenant_id=state["tenant_id"],
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=final_text,
        )
        await run_memory_consolidation(
            memory_conn,
            tenant_id=state["tenant_id"],
            user_id=user_id,
            user_input=state["question"],
            assistant_output=final_text,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        return {}

    async def planner_node(state: AgentState) -> dict[str, Any]:
        messages = state.get("planner_messages")
        if not messages:
            messages = [{"role": "system", "content": _PLANNER_SYSTEM_PROMPT}]
            term_guard_context = state.get("term_guard_context")
            if term_guard_context:
                messages.append({"role": "system", "content": term_guard_context})
            messages.extend(state.get("memory_context_messages", []))
            messages.append({"role": "user", "content": state["question"]})
        return await run_planner_turn(
            {**state, "planner_messages": messages},
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            max_tool_call_rounds=max_tool_call_rounds,
        )

    async def tool_call_node(state: AgentState) -> dict[str, Any]:
        return await run_tool_calls(
            state,
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

    async def planner_responder_node(state: AgentState) -> dict[str, Any]:
        # Planner 最后一轮返回的文本已经是基于全部工具结果生成的答案，这里
        # 只做格式化/字段补全，不再发起第二次 LLM 调用（省一次调用的钱），
        # 见 docs/AGENT_PLANNER_DESIGN.md §6.2。
        return {
            "answer_text": state.get("answer_text", ""),
            "fallback_triggered": False,
        }

    def route_after_input_safety(state: AgentState) -> str:
        return "term_guard" if state.get("is_input_safe", True) else "output_safety"

    def route_after_retrieval(state: AgentState) -> str:
        return "responder" if state.get("retrieved_records") else "fallback"

    graph: StateGraph[AgentState] = StateGraph(AgentState)
    graph.add_node("input_safety", input_safety_node)
    graph.add_node("term_guard", term_guard_node)
    graph.add_node("memory_recall", memory_recall_node)
    graph.add_node("fallback", fallback_node)
    graph.add_node("create_ticket", create_ticket_node)
    graph.add_node("output_safety", output_safety_node)
    graph.add_node("memory_save", memory_save_node)

    graph.add_edge(START, "input_safety")
    graph.add_conditional_edges(
        "input_safety",
        route_after_input_safety,
        {"term_guard": "term_guard", "output_safety": "output_safety"},
    )
    graph.add_edge("term_guard", "memory_recall")
    graph.add_edge("fallback", "create_ticket")
    graph.add_edge("create_ticket", "output_safety")
    graph.add_edge("output_safety", "memory_save")
    graph.add_edge("memory_save", END)

    if enable_autonomous_planning:
        graph.add_node("planner", planner_node)
        graph.add_node("tool_call", tool_call_node)
        graph.add_node("planner_responder", planner_responder_node)

        graph.add_edge("memory_recall", "planner")
        graph.add_conditional_edges(
            "planner",
            route_after_planner,
            {
                "tool_call": "tool_call",
                "responder": "planner_responder",
                "fallback": "fallback",
            },
        )
        graph.add_edge("tool_call", "planner")
        graph.add_edge("planner_responder", "output_safety")
    else:
        graph.add_node("retrieval", retrieval_node)
        graph.add_node("responder", responder_node)

        graph.add_edge("memory_recall", "retrieval")
        graph.add_conditional_edges(
            "retrieval",
            route_after_retrieval,
            {"responder": "responder", "fallback": "fallback"},
        )
        graph.add_edge("responder", "output_safety")

    return graph.compile()
