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
