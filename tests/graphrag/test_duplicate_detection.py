from app.graphrag.duplicate_detection import (
    find_duplicate_pairs,
    find_similar_terms,
    has_identifier_conflict,
    identifier_like_fields,
    term_similarity_score,
)
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


def _customer(key: str, name: str, **props) -> Term:
    return Term(
        tenant_id="t1", node_key=key, standard_name=name, aliases=[],
        term_type="客户", extra_properties=props,
    )


def _population(**extra) -> list[Term]:
    """八个手机号各不相同、会员等级大量重复的客户——让"手机号像标识、
    会员等级不像"这件事从数据里算得出来。"""
    others = [
        _customer(f"c{i}", f"客户{i}", phone=f"1380000000{i}", level="金卡" if i % 2 else "银卡")
        for i in range(6)
    ]
    return others


def test_same_name_different_phone_is_not_a_duplicate():
    """两个都叫「张伟」的客户，手机号不同——是两个人，不推。

    名字打分 1.0。不挡住的话同名不同人会把疑似重复队列刷满，而审核员一旦
    发现队列里大半是误报，就会开始不看它——真重复也跟着没人看了。
    """
    a = _customer("a", "张伟", phone="13911112222", level="金卡")
    b = _customer("b", "张伟", phone="13933334444", level="金卡")

    pairs = find_duplicate_pairs(_population() + [a, b])

    assert not any({p[0].node_key, p[1].node_key} == {"a", "b"} for p in pairs)


def test_same_name_differing_only_in_a_non_identifier_field_is_still_suggested():
    """会员等级不同照样推——那种字段真重复之间本来就常常不一样。

    来自不同数据源的同一个人，A 表是旧等级、B 表是新等级：那正是属性冲突
    队列存在的原因。"任意字段不同就不是重复"会把真重复也压掉。没有这一条
    的话，那种实现也能让上面那条通过。
    """
    a = _customer("a", "张伟", phone="13911112222", level="金卡")
    b = _customer("b", "张伟", phone="13911112222", level="银卡")

    pairs = find_duplicate_pairs(_population() + [a, b])

    assert any({p[0].node_key, p[1].node_key} == {"a", "b"} for p in pairs)


def test_one_side_missing_the_identifier_is_not_a_conflict():
    """一边有手机号一边没有，不算反证——缺失只是信息不全。"""
    a = _customer("a", "张伟", phone="13911112222")
    b = _customer("b", "张伟")

    pairs = find_duplicate_pairs(_population() + [a, b])

    assert any({p[0].node_key, p[1].node_key} == {"a", "b"} for p in pairs)


def test_identifier_fields_are_inferred_from_the_data():
    """哪个字段像标识，是从值的重复程度算出来的，不靠配置。"""
    fields = identifier_like_fields(_population())

    assert "phone" in fields
    assert "level" not in fields


def test_too_few_values_are_not_enough_to_call_a_field_an_identifier():
    """三条数据里的三个值当然互不相同，但那说明不了这个字段是标识。

    不设下限的话，小分组里每个字段都会被当成标识，于是任何一点属性差异都
    会把真重复压掉。
    """
    few = [_customer(f"c{i}", f"客户{i}", level=f"等级{i}") for i in range(3)]

    assert identifier_like_fields(few) == set()


def test_integral_float_and_int_are_the_same_value():
    """同一个编码一边是 ETL 读出的 42.0、一边是界面填的 42，不是冲突。"""
    a = _customer("a", "张伟", code=42.0)
    b = _customer("b", "张伟", code=42)

    assert has_identifier_conflict(a, b, {"code"}) is False
