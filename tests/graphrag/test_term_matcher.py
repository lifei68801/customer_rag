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
