from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable

import aiosqlite
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.create_ticket_tool import create_ticket
from app.agent.planner import route_after_planner, run_planner_turn, run_tool_calls
from app.agent.state import AgentState
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.term_guard import build_term_guard_context
from app.memory.clarification import (
    clear_pending_clarification,
    ensure_clarification_schema,
    get_pending_clarification,
    looks_like_a_time_reply,
    merge_clarification_reply,
    set_pending_clarification,
)
from app.memory.action_executor import apply_memory_actions
from app.memory.conflict_resolver import resolve_memory_actions
from app.memory.consolidation_queue import enqueue_consolidation_job
from app.memory.context_injection import inject_memory_context
from app.memory.correction_intent import detect_correction_intent
from app.memory.fact_extractor import extract_facts
from app.memory.session_window_store import SessionWindowStore, SQLiteSessionWindowStore
from app.memory.similarity import find_similar_memory_items
from app.memory.structured_recall import search_turns_by_keyword_and_window
from app.memory.temporal_resolver import resolve_time_window
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorStore
from app.safety.leakage_detection import detect_internal_leakage
from app.safety.prompt_injection import detect_prompt_injection, wrap_system_prompt
from app.safety.rules import UNSAFE_INPUT_MESSAGE, UNSAFE_OUTPUT_MESSAGE, check_text
from app.safety.semantic_review import semantic_safety_review
from app.voice.streaming_responder import stream_sentences

_PROMPT_TEMPLATE = "根据以下资料回答问题。\n资料：\n{context}\n\n问题：{question}"
_FALLBACK_MESSAGE = "抱歉，暂时没有找到确切答案，已为您转接人工客服处理。"
_FUTURE_TIME_CLARIFICATION_PROMPT = (
    "您提到的时间似乎是将来的日期，我们暂时没有对应的记录。"
    "请问您想查询的是哪一个具体的历史日期呢？"
)
_LITE_SAFETY_FALLBACK_SENTENCE = "（该部分内容因安全检查被过滤。）"
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
    ticket_conn: aiosqlite.Connection | None = None,
    top_k: int = 3,
    min_relevance_score: float | None = None,
    enable_autonomous_planning: bool = False,
    max_tool_call_rounds: int = 3,
    on_answer_chunk: Callable[[str], Awaitable[None]] | None = None,
    session_window_store: SessionWindowStore | None = None,
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
    并在 OutputSafety 后保存本轮对话+把 consolidation 任务异步入队
    （app/memory/consolidation_queue.py），不阻塞本轮响应——真正的事实
    抽取+冲突决策+写入由独立的 app/memory/consolidation_worker.py
    处理，需要部署方单独调度（cron/systemd timer/常驻循环）。

    memory_conn 提供时，检索前的 query 改写（app/qa/query_rewrite.py）也
    会看到 memory_context_messages（近期会话轮次），用于补全"这个报错"
    这类模糊指代——之前改写只看孤立的当前问题，看不到对话历史。

    ticket_conn 同样可选：不传则 create_ticket 保持纯 mock 行为，不落库；
    传入则工单持久化，使 app/memory/proactive_scan.py 能扫描出"挂起过久"
    的工单并触发主动跟进（见 app/memory/followup_engine.py）。

    min_relevance_score 为可选项：真实向量库（Milvus）几乎总能返回 Top-K
    个最近邻，哪怕语义上完全不相关，光看"检索结果是否为空"判断该不该转
    人工远远不够——真实数据测试就踩到过这个问题。设置后，检索到的记录
    即使非空，只要最高分（VectorRecord.score）低于这个阈值也会走
    fallback+create_ticket，而不是把不相关的资料硬塞给 LLM。不设置
    （默认）则完全不影响原有行为，只看结果是否为空。

    未配置 rerank_provider 时，score 是向量检索阶段的余弦相似度，此时
    BM25-only 命中没有可比较的分数，不参与这个判断，给它们"疑罪从无"；
    配置了 rerank_provider 时，score 会被 hybrid_search 覆盖成 rerank
    返回的 relevance_score（见 app/retrieval/hybrid_search.py），此时
    BM25-only 命中也会拿到真实分数、一并参与阈值判断，不再自动豁免。

    时间/澄清状态机同样依赖 memory_conn（未提供则完全跳过，行为不变）：
    检索兜底（fallback）时如果问题里提到未来时间，不直接转人工工单，而是
    先记录待澄清状态并追问具体日期；下一轮如果用户只回复了一个时间，
    会自动和原问题拼接后重新走完整流程。刻意只在"检索本来就要失败"这
    条路径上做判断，而不是在每个问题进入检索前都做时间解析——大量带
    "明天/下周"的问题其实是在问政策性内容（"明天能退货吗"），不是在查
    历史数据，全局拦截会误伤这类正常提问；只在兜底路径上介入，风险
    仅限于"反正要失败"的场景。

    on_answer_chunk 为可选项：不提供则 Responder 仍是一次性 complete()
    调用（行为不变）；提供且当前 llm_provider_name 对应的 provider 支持
    stream_complete() 时，Responder 改为流式生成，按句子边界（复用
    app/voice/streaming_responder.py 的 stream_sentences，和语音输出
    同一套切句逻辑）逐句调用 on_answer_chunk。每句先过一次轻量规则检查
    （check_text，毫秒级，不调 LLM），命中风险词的句子替换成安全兜底话术
    再推送，而不是原样播报——完整语义级安全审查（OutputSafety 里的
    semantic_safety_review）仍然在整段回复生成完毕后照常运行一次，这是
    防御纵深，不是用轻量检查取代它。这一权衡（先播出部分内容、完整审查
    在后）已与产品方确认，如果 provider 不支持流式，自动退化为一次性
    生成+审查，不会强行流式。

    session_window_store 为可选项：不传（默认）则等价于用
    SQLiteSessionWindowStore 包装同一个 memory_conn，行为和接入前完全
    一致；传入（例如 Redis 实现）则 memory_save_node 写会话轮次时改用
    注入的 store，替代直接调用 app/memory/session_window.py 的自由函数
    append_turn。仅切换写入路径——memory_recall_node 读取近期会话轮次
    仍然走 context_injection.py 里的 get_recent_turns(memory_conn, ...)，
    读取路径的切换留给后续任务（先验证写入路径稳定，避免一次改两个
    方向增加排查复杂度）。
    """

    resolved_session_window_store: SessionWindowStore | None = None
    if memory_conn is not None:
        resolved_session_window_store = session_window_store or SQLiteSessionWindowStore(
            memory_conn
        )

    async def input_safety_node(state: AgentState) -> dict[str, Any]:
        result = check_text(state["question"], banned_terms=banned_terms)
        injection_result = detect_prompt_injection(state["question"])
        return {
            "is_input_safe": result.is_safe and not injection_result.is_suspicious,
            "input_unsafe_terms": result.matched_terms
            + injection_result.matched_categories,
        }

    async def correction_check_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is None:
            return {}
        question = state["question"]
        is_correction = await detect_correction_intent(
            question, llm_registry=llm_registry, llm_provider_name=llm_provider_name
        )
        if not is_correction:
            return {}

        tenant_id = state["tenant_id"]
        user_id = state.get("user_id", "")
        facts = await extract_facts(
            user_input=question,
            assistant_output="",
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        if not facts:
            return {}

        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=facts), provider_name=embedding_provider_name
        )
        candidates: dict[str, dict[str, Any]] = {}
        for vector in embed_result.vectors:
            similar = await find_similar_memory_items(
                memory_conn,
                tenant_id=tenant_id,
                user_id=user_id,
                query_vector=vector,
                top_k=20,
            )
            for item in similar:
                candidates[item["memory_id"]] = item

        actions = await resolve_memory_actions(
            new_facts=facts,
            existing_memories=list(candidates.values()),
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        applied = await apply_memory_actions(
            memory_conn,
            tenant_id=tenant_id,
            user_id=user_id,
            actions=actions,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
        )
        if not applied:
            return {}

        primary = applied[0]
        if primary["event"] in ("UPDATE", "DELETE"):
            confirmation = f"好的，已经帮您更正为：{primary['text']}" if primary["text"] else "好的，已经帮您更正了。"
        else:
            confirmation = f"好的，已经记下：{primary['text']}"
        # 返回 answer_text（而非直接写 final_text）：走和 responder_node 相同的
        # 契约，让 output_safety_node 按正常流程对确认文案做规则+语义双重
        # 安全审查后再产出 final_text——确认文案里拼接了用户刚提供的原始
        # 文本（primary["text"]来自事实抽取，未经审查），不能因为这是"确认
        # 消息"就绕开审查。fallback_triggered=False 是这个决定的关键开关：
        # output_safety_node 只在 fallback_triggered 为真时才跳过语义审查
        # （因为兜底文案是固定文案，不含生成内容），确认文案不满足这个前提。
        return {
            "is_correction_handled": True,
            "fallback_triggered": False,
            "answer_text": confirmation,
        }

    async def clarification_check_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is None:
            return {}
        await ensure_clarification_schema(memory_conn)
        session_id = state.get("session_id", "")
        pending = await get_pending_clarification(
            memory_conn,
            tenant_id=state["tenant_id"],
            session_id=session_id,
            now=datetime.now(),
        )
        if pending and looks_like_a_time_reply(state["question"]):
            merged = merge_clarification_reply(
                original_question=pending["original_question"],
                reply_text=state["question"],
            )
            await clear_pending_clarification(
                memory_conn, tenant_id=state["tenant_id"], session_id=session_id
            )
            return {"question": merged}
        return {}

    async def term_guard_node(state: AgentState) -> dict[str, Any]:
        if not (terms and graph_client is not None):
            return {"term_guard_context": None}
        context = await build_term_guard_context(
            state["question"],
            terms=terms,
            tenant_id=state["tenant_id"],
            graph_client=graph_client,
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
            question=state["question"],
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
        )
        # P1 结构化历史检索（架构文档 §6.3）：问题里带可解析的时间表达式
        # 时（"昨天""上周三"），额外按 tenant+user+时间窗口做一次跨 session
        # 的关键词过滤检索，把命中的历史轮次原文拼成独立的一条 system
        # message——不去改写/合并已有的 inject_memory_context 结果（近期
        # 会话轮次+长期记忆条目），两路结果职责不同、互不覆盖。没有可解析
        # 时间表达式，或窗口内没有关键词命中时完全是 no-op，不额外拼接
        # 任何内容，不影响接入前的既有行为。
        time_result = await resolve_time_window(
            state["question"],
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            reference_time=datetime.now(),
        )
        if time_result.resolved and time_result.start and time_result.end:
            structured_turns = await search_turns_by_keyword_and_window(
                memory_conn,
                tenant_id=state["tenant_id"],
                user_id=state.get("user_id", ""),
                start=time_result.start,
                end=time_result.end,
                question=state["question"],
            )
            if structured_turns:
                lines = "\n".join(
                    f"- {turn['role']}: {turn['content']}" for turn in structured_turns
                )
                messages.append(
                    {
                        "role": "system",
                        "content": f"以下是您在相关时间段提到的历史对话：\n{lines}",
                    }
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
            conversation_context=state.get("memory_context_messages", []),
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

        if on_answer_chunk is not None and llm_registry.supports_streaming(
            ProviderCapability.LLM, llm_provider_name
        ):
            text_stream = llm_registry.stream(
                ProviderCapability.LLM,
                ProviderRequest(messages=messages),
                provider_name=llm_provider_name,
            )
            sent_sentences: list[str] = []
            async for sentence in stream_sentences(text_stream):
                safety_result = check_text(sentence, banned_terms=banned_terms)
                safe_sentence = (
                    sentence if safety_result.is_safe else _LITE_SAFETY_FALLBACK_SENTENCE
                )
                await on_answer_chunk(safe_sentence)
                sent_sentences.append(safe_sentence)
            return {"answer_text": "".join(sent_sentences), "fallback_triggered": False}

        result = await llm_registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=messages),
            provider_name=llm_provider_name,
        )
        return {"answer_text": result.text, "fallback_triggered": False}

    async def fallback_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is not None:
            time_result = await resolve_time_window(
                state["question"],
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                reference_time=datetime.now(),
            )
            if time_result.resolved and time_result.is_future:
                await ensure_clarification_schema(memory_conn)
                await set_pending_clarification(
                    memory_conn,
                    tenant_id=state["tenant_id"],
                    session_id=state.get("session_id", ""),
                    original_question=state["question"],
                    clarification_prompt=_FUTURE_TIME_CLARIFICATION_PROMPT,
                    now=datetime.now(),
                )
                return {
                    "answer_text": _FUTURE_TIME_CLARIFICATION_PROMPT,
                    "fallback_triggered": True,
                    "needs_clarification": True,
                }
        return {
            "answer_text": _FALLBACK_MESSAGE,
            "fallback_triggered": True,
            "needs_clarification": False,
        }

    async def create_ticket_node(state: AgentState) -> dict[str, Any]:
        result = await create_ticket(
            tenant_id=state["tenant_id"],
            customer_id=state.get("user_id", ""),
            question=state["question"],
            reason="检索结果不足，需人工介入",
            conn=ticket_conn,
        )
        return {"ticket_id": result.ticket_id}

    async def output_safety_node(state: AgentState) -> dict[str, Any]:
        if not state.get("is_input_safe", True):
            return {"is_output_safe": True, "final_text": UNSAFE_INPUT_MESSAGE}
        answer = state.get("answer_text", "")
        result = check_text(answer, banned_terms=banned_terms)
        if not result.is_safe:
            return {"is_output_safe": False, "final_text": UNSAFE_OUTPUT_MESSAGE}
        leakage_result = detect_internal_leakage(answer)
        if leakage_result.is_leaked:
            return {"is_output_safe": False, "final_text": UNSAFE_OUTPUT_MESSAGE}

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
                "final_text": UNSAFE_OUTPUT_MESSAGE,
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
        assert resolved_session_window_store is not None
        await resolved_session_window_store.append_turn(
            tenant_id=state["tenant_id"],
            session_id=session_id,
            user_id=user_id,
            role="user",
            content=state["question"],
        )
        await resolved_session_window_store.append_turn(
            tenant_id=state["tenant_id"],
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=final_text,
        )
        if state.get("is_correction_handled"):
            # correction_check_node 已经在本轮同步完成了事实抽取+冲突决策+
            # 写入（见该节点注释），这里再入队只会让 worker 用独立的一次
            # LLM 重新抽取/决策同一对 (question, confirmation)——最好情况
            # 是空转，最坏情况是 worker 的独立判断和刚才的同步结果打架，
            # 写出重复或冲突的记忆条目。跳过入队，但上面的 append_turn 仍然
            # 要跑，这轮对话要正常出现在会话历史里。
            return {}
        # 只做一次快速 INSERT 入队，真正耗时的事实抽取+冲突决策+写入交给
        # app/memory/consolidation_worker.py 异步处理，不阻塞本轮响应
        # （见 docs/ARCHITECTURE.md §6.2"异步 consolidation 队列"）。
        await enqueue_consolidation_job(
            memory_conn,
            tenant_id=state["tenant_id"],
            user_id=user_id,
            session_id=session_id,
            user_input=state["question"],
            assistant_output=final_text,
        )
        return {}

    async def planner_node(state: AgentState) -> dict[str, Any]:
        messages = state.get("planner_messages")
        if not messages:
            messages = [
                {"role": "system", "content": wrap_system_prompt(_PLANNER_SYSTEM_PROMPT)}
            ]
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
        return (
            "correction_check" if state.get("is_input_safe", True) else "output_safety"
        )

    def route_after_correction_check(state: AgentState) -> str:
        return "output_safety" if state.get("is_correction_handled") else "clarification_check"

    def route_after_retrieval(state: AgentState) -> str:
        records = state.get("retrieved_records")
        if not records:
            return "fallback"
        if min_relevance_score is not None:
            scores = [record.score for record in records if record.score is not None]
            if scores and max(scores) < min_relevance_score:
                return "fallback"
        return "responder"

    def route_after_fallback(state: AgentState) -> str:
        return "output_safety" if state.get("needs_clarification") else "create_ticket"

    graph: StateGraph[AgentState] = StateGraph(AgentState)
    graph.add_node("input_safety", input_safety_node)
    graph.add_node("correction_check", correction_check_node)
    graph.add_node("clarification_check", clarification_check_node)
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
        {"correction_check": "correction_check", "output_safety": "output_safety"},
    )
    graph.add_conditional_edges(
        "correction_check",
        route_after_correction_check,
        {"output_safety": "output_safety", "clarification_check": "clarification_check"},
    )
    graph.add_edge("clarification_check", "term_guard")
    graph.add_edge("term_guard", "memory_recall")
    graph.add_conditional_edges(
        "fallback",
        route_after_fallback,
        {"create_ticket": "create_ticket", "output_safety": "output_safety"},
    )
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
