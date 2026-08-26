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


def test_find_duplicate_pairs_skips_purely_numeric_standard_names():
    # 纯数字标准名（如批量导入的"销量"术语 100/101/102...）两两之间按
    # LCS/较短字符串长度打分极易超阈值（"100"跟"101"共享"10"两个字符，
    # 2/3≈0.67），会把审核队列刷爆成几万条毫无意义的建议——这类术语跳过
    # 检测，不参与两两比对。
    numeric_a = Term(
        tenant_id="t1", node_key="销量:100", standard_name="100", aliases=[], term_type="销量",
    )
    numeric_b = Term(
        tenant_id="t1", node_key="销量:101", standard_name="101", aliases=[], term_type="销量",
    )

    pairs = find_duplicate_pairs([numeric_a, numeric_b])

    assert pairs == []


def test_find_duplicate_pairs_still_compares_non_numeric_terms():
    # 数字过滤不能误伤原有的非数字重复检测
    pairs = find_duplicate_pairs([_COCA, _PEPSI, _KEKOULE])
    assert len(pairs) == 1


def test_find_similar_terms_returns_empty_for_purely_numeric_candidate():
    numeric_existing = Term(
        tenant_id="t1", node_key="销量:101", standard_name="101", aliases=[], term_type="销量",
    )
    results = find_similar_terms("100", [numeric_existing])

    assert results == []


def test_find_similar_terms_excludes_purely_numeric_existing_terms():
    numeric_existing = Term(
        tenant_id="t1", node_key="销量:100", standard_name="100", aliases=[], term_type="销量",
    )
    results = find_similar_terms("非数字候选名100", [numeric_existing, _COCA])

    assert numeric_existing not in [r[0] for r in results]
