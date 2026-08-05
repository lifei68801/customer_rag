from __future__ import annotations

from app.graphrag.ontology import Term


def score_terminology_accuracy(answer: str, terms: list[Term]) -> float | None:
    """回答里提到的专有名词，是否用了标准名称而不是别名。

    只统计"回答里确实提到了该术语（标准名或任一别名）"的术语；一个都
    没提到时返回 None（不适用），不是 0 分或满分——这条评测用例对这个
    答案而言压根没有专有名词准确性的问题需要检验。
    """
    relevant = 0
    correct = 0
    for term in terms:
        candidates = [term.standard_name, *term.aliases]
        mentioned = any(c and c in answer for c in candidates)
        if not mentioned:
            continue
        relevant += 1
        if term.standard_name in answer:
            correct += 1
    if relevant == 0:
        return None
    return correct / relevant
