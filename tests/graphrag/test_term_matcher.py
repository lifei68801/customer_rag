from app.graphrag.ontology import Term
from app.graphrag.term_matcher import match_terms

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时", "E502"],
        term_type="error_code",
        product_line="核心平台",
    ),
    Term(
        standard_name="登录模块",
        aliases=["认证模块", "登录"],
        term_type="module",
        product_line="核心平台",
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
        standard_name="服务器连接超时",
        aliases=[],
        term_type="error_code",
        product_line="核心平台",
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
