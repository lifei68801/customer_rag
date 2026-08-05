from __future__ import annotations

from typing import TypedDict

from app.retrieval.vector_store import VectorRecord


class AgentState(TypedDict, total=False):
    question: str
    is_input_safe: bool
    input_unsafe_terms: list[str]
    term_guard_context: str | None
    retrieved_records: list[VectorRecord]
    used_sources: list[str]
    answer_text: str
    fallback_triggered: bool
    ticket_id: str | None
    is_output_safe: bool
    final_text: str
