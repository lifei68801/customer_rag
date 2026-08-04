from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    question: str
    expected_answer: str
    expected_sources: list[str]
