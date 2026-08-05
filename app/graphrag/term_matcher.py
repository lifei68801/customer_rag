from __future__ import annotations

from app.graphrag.ontology import Term


def match_terms(text: str, terms: list[Term]) -> list[Term]:
    """精确匹配：文本中出现术语的标准名称或任一别名，即命中该术语。

    这是 TermGuard 强制安全网的第一层判断——只做字面命中，不涉及
    向量相似度/编辑距离的模糊匹配（那属于更进阶的功能，需先用精确
    匹配跑出数据再评估是否值得加，避免引入误召回风险）。
    """
    matched: list[Term] = []
    for term in terms:
        candidates = [term.standard_name, *term.aliases]
        if any(candidate and candidate in text for candidate in candidates):
            matched.append(term)
    return matched
