from __future__ import annotations

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

    dp[i][j] = 「x[i-1] 与 y[j-1] 配对、且这是最后一个匹配」时能达到的最大匹配数。
    必须把"最后一个匹配落在哪"编进状态本身，而不是写成 dp[i][j]=前i前j的最大
    匹配数、再另外用一张 last[i][j] 记录位置——后者不是合法的 DP：在某个
    (i,j) 上贪心取最大长度，可能留下一个对后续间隔检查更不利的 last，导致
    整体反而更差。实测反例 matched_length("bbbbbbba", "baba", interval=2)
    在那种写法下返回 2，真实最优是 3。

    prefix_best[i][j] = max(dp[i][1..j])，让"上一个匹配落在 x 的第 i 个字符、
    且只用掉 y 的前 j 个字符"这一族状态能 O(1) 查到最好成绩。间隔约束
    (i-1) - (i2-1) - 1 <= interval 化简成 i2 >= i - interval - 1，所以往回
    只需要看 interval+1 个位置。
    """
    x, y = a.lower(), b.lower()
    if not x or not y:
        return 0
    n, m = len(x), len(y)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    prefix_best = [[0] * (m + 1) for _ in range(n + 1)]
    answer = 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if x[i - 1] == y[j - 1]:
                # 上一个匹配要么不存在（这是起点，prev=0），要么落在
                # x 的 [i-interval-1, i-1] 号位置上。
                prev = 0
                for i2 in range(max(1, i - interval - 1), i):
                    if prefix_best[i2][j - 1] > prev:
                        prev = prefix_best[i2][j - 1]
                dp[i][j] = prev + 1
                if dp[i][j] > answer:
                    answer = dp[i][j]
            prefix_best[i][j] = max(prefix_best[i][j - 1], dp[i][j])
    return answer


def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
    """滑动窗口 + 带间隔约束的子序列匹配，判断 candidate 是否在 text 里有
    足够相似的片段。

    窗口长度等于候选词长度，逐位置滑动。因为窗口长度恒等于 len(candidate)，
    这里的"匹配长度/窗口长度"和"匹配长度/候选名长度"是同一个值——也就是
    precision 恒等于 recall，所以不需要像召回侧（ontology_recall.
    fbeta_match_score）那样引入 F-beta 双向评分，直接用比例即可。
    """
    window = len(candidate)
    if window == 0 or len(text) < window:
        return False
    for i in range(len(text) - window + 1):
        if matched_length(text[i : i + window], candidate) / window >= threshold:
            return True
    return False


def match_terms(
    text: str, terms: list[Term], *, fuzzy_threshold: float = 0.75
) -> list[Term]:
    """精确匹配 + 模糊匹配兜底：文本中出现术语的标准名称或任一别名
    （原样出现，或足够相似）即命中该术语。

    这是 TermGuard 强制安全网的第一层判断。精确匹配（字面子串出现）
    优先；某个术语的所有候选名都没有精确命中时，再用滑动窗口 +
    matched_length（带间隔约束的子序列匹配）兜底一次。模糊命中不经过 LLM 二次
    确认，直接和精确命中一样触发强制注入——TermGuard 误命中的代价
    很轻（多塞一段可能不相关的图谱上下文，不像 ASR 校正那样直接
    改写用户输入文本），保持"TermGuard 不依赖 LLM 自主判断"这条
    架构设计的核心原则。

    fuzzy_threshold 默认 0.75（比 ASR 校正的 0.6 更保守，因为这里没有
    LLM 兜底误召回）——这是参考起点，需要结合真实数据调整，不是权威值。

    已知代价：短码型候选（如错误码 "E502"，4 个字符）在 0.75 阈值下
    分辨率很低——只差 1 位的同族码（如 "E503"）匹配比例恰好等于 0.75
    （4 个字符里匹配上 3 个），
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
