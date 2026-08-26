from app.graphrag.duplicate_detection import find_duplicate_pairs, find_similar_terms, term_similarity_score
from app.graphrag.ontology import Term

_COCA = Term(
    tenant_id="t1", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
    aliases=["可口可乐", "coke"], term_type="公司",
)
_PEPSI = Term(
    tenant_id="t1", node_key="公司:Pepsi", standard_name="Pepsi",
    aliases=["百事"], term_type="公司",
)
_KEKOULE = Term(
    tenant_id="t1", node_key="公司:可口可乐股份", standard_name="可口可乐",
    aliases=[], term_type="公司",
)


def test_term_similarity_score_matches_via_alias():
    # "可口可乐"（_KEKOULE 的 standard_name）精确等于 _COCA 的一个 alias——
    # 应该拿到满分，而不是 _COCA.standard_name="Coca-Cola" 跟
    # _KEKOULE.standard_name="可口可乐" 那种中英文零重合的低分。
    score = term_similarity_score(_COCA, _KEKOULE)
    assert score == 1.0


def test_term_similarity_score_low_for_unrelated_terms():
    score = term_similarity_score(_COCA, _PEPSI)
    assert score < 0.6


def test_find_similar_terms_finds_alias_match():
    results = find_similar_terms("可口可乐", [_COCA, _PEPSI])

    assert len(results) == 1
    assert results[0][0] is _COCA
    assert results[0][1] == 1.0


def test_find_similar_terms_excludes_below_threshold():
    results = find_similar_terms("完全不相关的名字", [_COCA, _PEPSI])
    assert results == []


def test_find_similar_terms_sorted_by_score_descending():
    close_match = Term(
        tenant_id="t1", node_key="公司:可口可乐科技", standard_name="可口可乐科技",
        aliases=[], term_type="公司",
    )
    results = find_similar_terms("可口可乐", [close_match, _COCA])

    assert [r[0] for r in results] == [_COCA, close_match]


def test_find_duplicate_pairs_finds_one_pair_deterministic_order():
    pairs = find_duplicate_pairs([_COCA, _PEPSI, _KEKOULE])

    assert len(pairs) == 1
    term_a, term_b, score = pairs[0]
    # node_key 字符串排序保证确定性，不依赖输入列表顺序
    assert term_a.node_key < term_b.node_key
    assert {term_a.node_key, term_b.node_key} == {"公司:Coca-Cola", "公司:可口可乐股份"}
    assert score == 1.0


def test_find_duplicate_pairs_no_pairs_when_all_distinct():
    pairs = find_duplicate_pairs([_COCA, _PEPSI])
    assert pairs == []


def test_find_duplicate_pairs_order_independent():
    pairs_a = find_duplicate_pairs([_COCA, _PEPSI, _KEKOULE])
    pairs_b = find_duplicate_pairs([_KEKOULE, _COCA, _PEPSI])

    assert pairs_a == pairs_b
