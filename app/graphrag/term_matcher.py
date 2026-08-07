from __future__ import annotations

import difflib

from app.graphrag.ontology import Term


def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
    """滑动窗口 + difflib 相似度，判断 candidate 是否在 text 里有足够相似的片段。

    窗口长度等于候选词长度，逐位置滑动计算相似度比值——算法复用
    app/voice/asr_term_correction.py::_find_fuzzy_candidates 的核心逻辑。
    """
    window = len(candidate)
    if window == 0 or len(text) < window:
        return False
    for i in range(len(text) - window + 1):
        span = text[i : i + window]
        ratio = difflib.SequenceMatcher(None, span, candidate).ratio()
        if ratio >= threshold:
            return True
    return False


def match_terms(
    text: str, terms: list[Term], *, fuzzy_threshold: float = 0.75
) -> list[Term]:
    """精确匹配 + 模糊匹配兜底：文本中出现术语的标准名称或任一别名
    （原样出现，或足够相似）即命中该术语。

    这是 TermGuard 强制安全网的第一层判断。精确匹配（字面子串出现）
    优先；某个术语的所有候选名都没有精确命中时，再用滑动窗口 +
    difflib.SequenceMatcher 相似度兜底一次。模糊命中不经过 LLM 二次
    确认，直接和精确命中一样触发强制注入——TermGuard 误命中的代价
    很轻（多塞一段可能不相关的图谱上下文，不像 ASR 校正那样直接
    改写用户输入文本），保持"TermGuard 不依赖 LLM 自主判断"这条
    架构设计的核心原则。

    fuzzy_threshold 默认 0.75（比 ASR 校正的 0.6 更保守，因为这里没有
    LLM 兜底误召回）——这是参考起点，需要结合真实数据调整，不是权威值。

    已知代价：短码型候选（如错误码 "E502"，4 个字符）在 0.75 阈值下
    分辨率很低——只差 1 位的同族码（如 "E503"）相似度恰好等于 0.75，
    会被判定为模糊命中，注入的是形近但错误的错误码上下文。这是当前
    阈值下被接受的已知行为（见
    tests/graphrag/test_term_matcher.py::test_match_terms_known_false_positive_for_similar_short_error_codes），
    调整阈值前需要先确认不会反过来漏掉真实的模糊变体场景（如"服务器
    连接超时"打错1字后的相似度约 0.857）。
    """
    matched: list[Term] = []
    for term in terms:
        candidates = [term.standard_name, *term.aliases]
        if any(candidate and candidate in text for candidate in candidates):
            matched.append(term)
            continue
        if any(
            candidate
            and _has_fuzzy_match(text, candidate, threshold=fuzzy_threshold)
            for candidate in candidates
        ):
            matched.append(term)
    return matched
