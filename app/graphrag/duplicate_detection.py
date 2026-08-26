from __future__ import annotations

from app.graphrag.ontology import Term
from app.graphrag.ontology_recall import longest_common_substring_score

_DUPLICATE_SIMILARITY_THRESHOLD = 0.6


def _names_of(term: Term) -> list[str]:
    return [term.standard_name, *term.aliases]


def term_similarity_score(a: Term, b: Term) -> float:
    """两条术语的相似度——比对范围是各自的 standard_name + 全部 aliases，
    两两取最高分；longest_common_substring_score 本身对 a/b 不对称
    （除以 len(b)），这里取两个方向的最大值。"""
    names_a = _names_of(a)
    names_b = _names_of(b)
    return max(
        (
            max(
                longest_common_substring_score(name_a, name_b),
                longest_common_substring_score(name_b, name_a),
            )
            for name_a in names_a
            for name_b in names_b
        ),
        default=0.0,
    )


def find_similar_terms(
    candidate_name: str, existing_terms: list[Term]
) -> list[tuple[Term, float]]:
    """candidate_name 是一个裸字符串（尚未创建成 Term），跟 existing_terms
    里每一条的 standard_name/aliases 比对，返回超过阈值的 (Term, score)，
    按 score 降序排列。"""
    scored = []
    for term in existing_terms:
        score = max(
            (
                max(
                    longest_common_substring_score(candidate_name, name),
                    longest_common_substring_score(name, candidate_name),
                )
                for name in _names_of(term)
            ),
            default=0.0,
        )
        if score >= _DUPLICATE_SIMILARITY_THRESHOLD:
            scored.append((term, score))
    scored.sort(key=lambda item: (-item[1], item[0].standard_name))
    return scored


def find_duplicate_pairs(terms: list[Term]) -> list[tuple[Term, Term, float]]:
    """对 terms 两两比对，返回超过阈值的 (term_a, term_b, score)——term_a/
    term_b 按 node_key 字符串排序，保证同一对术语的结果跟输入列表顺序无关。
    调用方负责先按 term_type 分组再传进来，这个函数本身不做分组。"""
    pairs: list[tuple[Term, Term, float]] = []
    for i in range(len(terms)):
        for j in range(i + 1, len(terms)):
            score = term_similarity_score(terms[i], terms[j])
            if score >= _DUPLICATE_SIMILARITY_THRESHOLD:
                term_a, term_b = sorted([terms[i], terms[j]], key=lambda t: t.node_key)
                pairs.append((term_a, term_b, score))
    return pairs
