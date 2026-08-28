from __future__ import annotations

from typing import Any, TypedDict

from app.retrieval.vector_store import VectorRecord


class AgentState(TypedDict, total=False):
    question: str
    tenant_id: str
    session_id: str
    user_id: str
    is_input_safe: bool
    input_unsafe_terms: list[str]
    term_guard_context: str | None
    memory_context_messages: list[dict[str, str]]
    # Layer 1（app/qa/query_rewrite.py::resolve_question）消解指代之后的问题。
    # 下游 planner/ToolContext/确定性检索路径都用这个而不是原始 question，
    # 这样跨轮次指代只在一处解决一次，不用每个下游各自去猜。resolve_question
    # 节点没跑过或失败时不写这个字段，读取方用 state.get(...) 兜底回原始
    # question。
    resolved_question: str
    # 当前问题（消解指代后）跟历史里某一轮已经问过并回答过的问题基本相同时，
    # 记录那一轮的原始提问文本，供 planner_node 生成一条"这可能是重复提问"
    # 的软提示；没命中时不写这个字段。
    duplicate_of: str | None
    retrieved_records: list[VectorRecord]
    used_sources: list[str]
    answer_text: str
    fallback_triggered: bool
    ticket_id: str | None
    is_output_safe: bool
    semantic_review_reviewed: bool
    final_text: str
    needs_clarification: bool
    is_correction_handled: bool

    # Planner/ToolCall 循环专用字段（见 docs/AGENT_PLANNER_DESIGN.md §5）。
    # 只在 enable_autonomous_planning=True 时被 planner_node/tool_call_node 使用；
    # 静态确定性路径不读写这些字段。
    planner_messages: list[dict[str, str]]
    tool_call_round: int
    pending_tool_calls: list[dict[str, str]]
    tool_results: list[dict[str, Any]]
    planner_gave_up: bool
    # 本轮对话里 Planner 每一轮已经流式推送给用户的文本，按轮次顺序累积
    # （包括工具调用轮里那些从未进入 answer_text 的"前置说明文字"）——
    # output_safety_node 用这个字段做完整安全审查，而不是只审查最后一轮
    # 的 answer_text，因为更早的轮次也已经通过 on_answer_chunk 实时展示
    # 给用户看过了，只过了流式阶段的轻量规则检查是不够的。非流式/确定性
    # 路径不写这个字段。
    streamed_round_texts: list[str]
