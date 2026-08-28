from app.graphrag.ontology import Term
from app.graphrag.term_matcher import matched_length, match_terms

_TERMS = [
    Term(
        tenant_id="t1",
        node_key="错误码E502",
        standard_name="错误码E502",
        aliases=["网关超时", "E502"],
        term_type="error_code",
    ),
    Term(
        tenant_id="t1",
        node_key="登录模块",
        standard_name="登录模块",
        aliases=["认证模块", "登录"],
        term_type="module",
    ),
]


def test_match_terms_finds_standard_name_via_alias():
    matches = match_terms("我这边报了网关超时，应该怎么办", _TERMS)

    assert [m.standard_name for m in matches] == ["错误码E502"]


def test_match_terms_returns_empty_when_no_alias_or_name_present():
    matches = match_terms("今天天气怎么样", _TERMS)

    assert matches == []


def test_match_terms_dedupes_when_multiple_aliases_of_same_term_present():
    matches = match_terms("E502 也就是网关超时的意思吧", _TERMS)

    assert [m.standard_name for m in matches] == ["错误码E502"]


_FUZZY_TERMS = [
    Term(
        tenant_id="t1",
        node_key="服务器连接超时",
        standard_name="服务器连接超时",
        aliases=[],
        term_type="error_code",
    ),
]


def test_match_terms_finds_fuzzy_variant_within_threshold():
    # "连接" 打成了同音的"链接"，7 个字里错 1 个字，difflib.SequenceMatcher
    # 相似度约 0.857（(7-1)/7），高于默认阈值 0.75，应该被模糊层命中。
    matches = match_terms("最近老是提示服务器链接超时，是不是坏了", _FUZZY_TERMS)

    assert [m.standard_name for m in matches] == ["服务器连接超时"]


def test_match_terms_does_not_fuzzy_match_below_threshold():
    # 完全不相关的文本，相似度接近 0，远低于阈值，不应该被误命中。
    matches = match_terms("今天天气怎么样", _FUZZY_TERMS)

    assert matches == []


def test_match_terms_does_not_duplicate_exact_match_via_fuzzy_layer():
    # 文本里已经精确包含标准名，结果里这个术语只应该出现一次
    # （不会因为同时被模糊层重复命中而出现两次）。
    matches = match_terms("服务器连接超时了，麻烦看一下", _FUZZY_TERMS)

    assert [m.standard_name for m in matches] == ["服务器连接超时"]


_FUZZY_TERMS_WITH_ALIAS = [
    Term(
        tenant_id="t1",
        node_key="服务器连接超时",
        standard_name="服务器连接超时",
        aliases=["连接超时"],
        term_type="error_code",
    ),
]


def test_match_terms_known_false_positive_for_similar_short_error_codes():
    # 已知代价（见 term_matcher.py::match_terms 的 docstring）：短码候选
    # "E502" 和 "E503" 相似度恰好等于默认阈值 0.75，会被判定为模糊命中。
    # 这条测试锁定该已知行为，不是期望修复的 bug——调整阈值前先看这里。
    matches = match_terms("我遇到了E503错误", _TERMS)

    assert [m.standard_name for m in matches] == ["错误码E502"]


def test_match_terms_does_not_fuzzy_match_within_threshold_band():
    # "网络"替换了"连接"，7个字里错2个字，相似度约0.714，低于默认阈值
    # 0.75 但明显高于完全不相关文本的相似度——专门压中阈值边界区间，
    # 证明阈值没设太松（不同于 test_match_terms_does_not_fuzzy_match_below_threshold
    # 用的完全无关文本，那条测试证明不了这一点）。
    matches = match_terms("最近老是提示服务器网络超时，是不是坏了", _FUZZY_TERMS)

    assert matches == []


def test_match_terms_finds_fuzzy_variant_via_alias():
    # 模糊层同样要对别名生效，不能只对标准名生效。"链接超时"是别名
    # "连接超时"打错1字的变体，相似度0.75，应该通过别名命中。
    matches = match_terms("最近提示链接超时，你看一下", _FUZZY_TERMS_WITH_ALIAS)

    assert [m.standard_name for m in matches] == ["服务器连接超时"]


_MIXED_CASE_TERMS = [
    Term(
        tenant_id="t1", node_key="公司:Coca-Cola",
        standard_name="Coca-Cola", aliases=[], term_type="公司",
    ),
    Term(
        tenant_id="t1", node_key="产品:Cola",
        standard_name="Cola", aliases=[], term_type="产品",
    ),
]


def test_match_terms_is_case_insensitive_for_exact_match():
    matches = match_terms("请问COCA-COLA公司的联系方式", _MIXED_CASE_TERMS)

    assert "Coca-Cola" in [m.standard_name for m in matches]


def test_match_terms_is_case_insensitive_for_fuzzy_match():
    # 2026-08-27 真实案例：用户把"Coca-Cola"打成全小写的"coke-cola"（拼写
    # 也有出入）。大小写不敏感之前，case-sensitive 比较下"Coca-Cola"
    # （9字符，多处大小写不一致）相似度约 0.55，远低于 0.75 阈值完全
    # 匹配不上；只有短候选"Cola"（词尾巧合只差大小写）勉强压线命中，
    # 导致强制注入的是错误的实体（产品而不是公司）。忽略大小写后两者
    # 都应该正确命中。
    matches = match_terms("coke-cola公司有多少个订单", _MIXED_CASE_TERMS)

    assert "Coca-Cola" in [m.standard_name for m in matches]


def test_match_terms_respects_custom_fuzzy_threshold():
    # fuzzy_threshold 是可调的公开参数——用非默认值验证它真的生效。
    # 同一段文本在默认阈值0.75下命中（见
    # test_match_terms_finds_fuzzy_variant_within_threshold），但传入更
    # 严格的 fuzzy_threshold=0.9 时相似度0.857不够，应该不命中。
    matches = match_terms(
        "最近老是提示服务器链接超时，是不是坏了", _FUZZY_TERMS, fuzzy_threshold=0.9
    )

    assert matches == []


def test_matched_length_counts_in_order_characters_skipping_one_typo():
    # "服务器链接超时" 相对 "服务器连接超时" 只错了「连→链」一个字，
    # 按顺序能匹配上其余 6 个字（服务器+接超时）。最长公共【子串】只能
    # 数出「服务器」=3（后面的「接超时」被错字隔断成另一段而丢弃），
    # 这正是这个函数要解决的问题。
    assert matched_length("服务器链接超时", "服务器连接超时") == 6


def test_matched_length_distinguishes_one_typo_from_two():
    # 错1字能匹配6个，错2字只能匹配5个——这个差异是 term_guard 判定
    # "该不该命中" 的全部依据，必须被保留下来。
    one_typo = matched_length("服务器链接超时", "服务器连接超时")
    two_typos = matched_length("服务器网络超时", "服务器连接超时")
    assert one_typo == 6
    assert two_typos == 5
    assert one_typo > two_typos


def test_matched_length_interval_blocks_characters_scattered_across_text():
    # 「服务器连接超时」这 7 个字确实全部按顺序出现在这句无关的话里
    # （服-务-...-器-...-连-...-接-...-超-...-时），不加间隔约束的话
    # 会被判成完全匹配（7），这是纯最长公共子序列的致命误匹配。
    # interval=2 要求相邻两个匹配字符之间最多只能跨 2 个字符，把这种
    # 散落匹配切断。
    decoy = "服务里有个器件，连着接口，超过时限了"
    assert matched_length(decoy, "服务器连接超时", interval=99) == 7
    assert matched_length(decoy, "服务器连接超时", interval=2) == 5


def test_matched_length_is_case_insensitive():
    assert matched_length("COKE-COLA", "coke-cola") == 9


def test_matched_length_returns_zero_for_empty_input():
    assert matched_length("", "abc") == 0
    assert matched_length("abc", "") == 0


def test_matched_length_agrees_with_brute_force_on_repeated_characters():
    # 2026-08-28：第一版实现用 dp[i][j]=前i前j最大匹配数 + 另一张 last[i][j]
    # 记录位置，在重复字符上会少数——贪心取最大长度可能留下对后续间隔检查
    # 更不利的 last。手挑的例子看不出这类错误（原有 5 条测试全过），只有跟
    # 穷举参考实现对拍才能锁住，所以这条测试用属性测试而不是固定用例。
    import itertools
    import random

    def brute_force(a: str, b: str, interval: int) -> int:
        a, b = a.lower(), b.lower()
        for size in range(min(len(a), len(b)), 0, -1):
            for combo in itertools.combinations(range(len(a)), size):
                picked = "".join(a[i] for i in combo)
                remaining = iter(b)
                if not all(ch in remaining for ch in picked):
                    continue
                if all(
                    combo[k + 1] - combo[k] - 1 <= interval
                    for k in range(len(combo) - 1)
                ):
                    return size
        return 0

    assert matched_length("bbbbbbba", "baba", interval=2) == 3

    random.seed(20260828)
    for _ in range(200):
        a = "".join(random.choice("abc") for _ in range(random.randint(3, 9)))
        b = "".join(random.choice("abc") for _ in range(random.randint(2, 6)))
        for interval in (0, 1, 2, 3):
            assert matched_length(a, b, interval=interval) == brute_force(a, b, interval), (
                f"a={a!r} b={b!r} interval={interval}"
            )


def test_match_terms_and_recall_agree_on_the_coke_cola_case():
    """两处匹配逻辑对同一段文本必须给出一致结论——这是"统一实体匹配"这个
    设计目标的直接回归锚点。

    2026-08-27 的真实 bug：同一句 "coke-cola公司有多少个订单"，term_guard
    只匹配到产品 Cola（漏掉公司 Coca-Cola）、把错误实体的图谱上下文强制
    注入，而召回侧其实两个都能召回——两处结论矛盾，且系统里没有任何一处
    会发现这种矛盾。
    """
    from app.graphrag.ontology_categories import TermTypeCategory
    from app.graphrag.ontology_recall import recall_ontology_candidates

    terms = [
        Term(tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
             aliases=[], term_type="公司"),
        Term(tenant_id="demo", node_key="产品:Cola", standard_name="Cola",
             aliases=[], term_type="产品"),
    ]
    question = "coke-cola公司有多少个订单"

    guard_hits = {t.standard_name for t in match_terms(question, terms)}
    recalled = {t.standard_name for t in recall_ontology_candidates(
        question, terms=terms,
        term_type_schema={
            "公司": TermTypeCategory(value="公司", extra_fields=[]),
            "产品": TermTypeCategory(value="产品", extra_fields=[]),
        },
        allowed_combinations=[],
    ).entities}

    assert "Coca-Cola" in guard_hits
    assert "Coca-Cola" in recalled
