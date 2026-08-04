from __future__ import annotations

from dataclasses import dataclass

from app.eval.dataset import EvalCase


def score_context_recall(case: EvalCase, retrieved_sources: list[str]) -> float:
    matched = sum(1 for s in case.expected_sources if s in retrieved_sources)
    return matched / len(case.expected_sources)


@dataclass(frozen=True)
class EvalRunResult:
    average_context_recall: float
    per_case_scores: list[float]


def run_eval(
    cases_with_retrieval: list[tuple[EvalCase, list[str]]],
) -> EvalRunResult:
    scores = [
        score_context_recall(case, retrieved)
        for case, retrieved in cases_with_retrieval
    ]
    average = sum(scores) / len(scores)
    return EvalRunResult(average_context_recall=average, per_case_scores=scores)
