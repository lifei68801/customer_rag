from __future__ import annotations

import difflib

from app.graphrag.ontology import Term


_DEFAULT_INTERVAL = 2


def matched_length(a: str, b: str, *, interval: int = _DEFAULT_INTERVAL) -> int:
    """b 的字符按顺序在 a 里最多能匹配上多少个，且相邻两个匹配字符在 a 中
    跨越的字符数不超过 interval。大小写不敏感。

    这是"最长公共子序列"加了间隔约束的版本。不加约束时，长文本里散落各处
    的字符也能被全部匹配上——实测"服务里有个器件，连着接口，超过时限了"
    对"服务器连接超时"能匹配满 7 个字，是纯子序列算法的致命误匹配来源；
    interval 把匹配限制在局部连贯的区域内。

    间隔取 2 是实测甜点：取 3 太松，压不掉上面那个散落误匹配；取 1 太紧，
    会误伤 "coke-cola"→"Coca-Cola" 这类正常拼写变体。机制借鉴 swiftagent
    dev/2.7.5 的 get_common_str.py::_get_max_sequence（那里是 token 级
    interval=3，这里按字符级、取 2）。

    为什么不用 difflib.SequenceMatcher 或最长公共【子串】：
    - 最长公共子串只保留单个最长连续片段，"服务器链接超时"和"服务器网络
      超时"对"服务器连接超时"都只能数出「服务器」=3，无法区分错1字和错2字。
    - SequenceMatcher 能区分，但它是 term_guard 侧的老实现，没有间隔约束
      这个旋钮，也无法被召回侧复用（召回侧要的是"匹配了多少个字符"这个
      原始量，好在上层套 F-beta，而不是一个已经归一化死的比值）。

    dp[i][j] = a 前 i 个字符与 b 前 j 个字符能匹配的最大长度；
    last[i][j] = 取得该最大长度时最后一个匹配字符在 a 中的下标——间隔约束
    需要知道"上一个匹配落在哪"，所以 last 必须跟着 dp 一起转移。
    """
    x, y = a.lower(), b.lower()
    if not x or not y:
        return 0
    n, m = len(x), len(y)
    no_match = -1
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    last = [[no_match] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best, best_last = dp[i - 1][j], last[i - 1][j]
            if dp[i][j - 1] > best:
                best, best_last = dp[i][j - 1], last[i][j - 1]
            if x[i - 1] == y[j - 1]:
                prev_len, prev_last = dp[i - 1][j - 1], last[i - 1][j - 1]
                within_interval = (
                    prev_last == no_match or (i - 1) - prev_last - 1 <= interval
                )
                if within_interval and prev_len + 1 >= best:
                    best, best_last = prev_len + 1, i - 1
            dp[i][j], last[i][j] = best, best_last
    return dp[n][m]


def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
    """滑动窗口 + difflib 相似度，判断 candidate 是否在 text 里有足够相似的片段。

    窗口长度等于候选词长度，逐位置滑动计算相似度比值——算法复用
    app/voice/asr_term_correction.py::_find_fuzzy_candidates 的核心逻辑。
    """
    window = len(candidate)
    if window == 0 or len(text) < window:
        return False
    candidate_lower = candidate.lower()
    for i in range(len(text) - window + 1):
        span = text[i : i + window]
        ratio = difflib.SequenceMatcher(None, span.lower(), candidate_lower).ratio()
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
    text_lower = text.lower()
    matched: list[Term] = []
    for term in terms:
        candidates = [term.standard_name, *term.aliases]
        if any(candidate and candidate.lower() in text_lower for candidate in candidates):
            matched.append(term)
            continue
        if any(
            candidate
            and _has_fuzzy_match(text, candidate, threshold=fuzzy_threshold)
            for candidate in candidates
        ):
            matched.append(term)
    return matched
