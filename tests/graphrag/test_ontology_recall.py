from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination
from app.graphrag.ontology_recall import (
    format_recall_candidates,
    longest_common_substring_score,
    recall_ontology_candidates,
)


def test_longest_common_substring_score_exact_match():
    assert longest_common_substring_score("coke-cola", "Cola") == 1.0


def test_longest_common_substring_score_partial_match():
    score = longest_common_substring_score("coke-cola", "Coca-Cola")
    assert abs(score - 5 / 9) < 1e-9


def test_longest_common_substring_score_case_insensitive():
    assert longest_common_substring_score("COLA", "cola") == 1.0


def test_longest_common_substring_score_below_min_overlap_returns_zero():
    # "x" 和 "xyz" 最长公共子串只有1个字符，低于最小重叠长度阈值，直接判0分，
    # 不能因为候选名字短就让归一化分数虚高。
    assert longest_common_substring_score("x", "xyz") == 0.0


def test_longest_common_substring_score_empty_candidate_returns_zero():
    assert longest_common_substring_score("cola", "") == 0.0


_COLA_TERM = Term(
    tenant_id="demo", node_key="产品:Cola", standard_name="Cola",
    aliases=[], term_type="产品",
)
_COCA_COLA_TERM = Term(
    tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
    aliases=[], term_type="公司",
)
_UNRELATED_TERM = Term(
    tenant_id="demo", node_key="用户名:Alice", standard_name="Alice",
    aliases=[], term_type="用户名",
)
_TERM_TYPE_SCHEMA = {
    "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
    "产品": TermTypeCategory(value="产品", extra_fields=[ExtraFieldSpec(name="price", value_type="number")]),
    "公司": TermTypeCategory(value="公司", extra_fields=[]),
    "用户名": TermTypeCategory(value="用户名", extra_fields=[]),
}
_ALLOWED_COMBINATIONS = [
    AllowedCombination(subject_term_type="订单号", relation_type="BELONG_TO", object_term_type="产品"),
    AllowedCombination(subject_term_type="产品", relation_type="BELONG_TO", object_term_type="公司"),
    AllowedCombination(subject_term_type="订单号", relation_type="ORDER_BY", object_term_type="用户名"),
]


def test_recall_ontology_candidates_finds_relevant_term_types_relations_and_entities():
    candidates = recall_ontology_candidates(
        "查询Coca-Cola这家公司名下有多少个订单",
        terms=[_COLA_TERM, _COCA_COLA_TERM, _UNRELATED_TERM],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert "公司" in candidates.term_types
    assert "订单号" in candidates.term_types
    assert AllowedCombination(
        subject_term_type="产品", relation_type="BELONG_TO", object_term_type="公司",
    ) in candidates.relations
    assert AllowedCombination(
        subject_term_type="订单号", relation_type="BELONG_TO", object_term_type="产品",
    ) in candidates.relations
    assert _COCA_COLA_TERM in candidates.entities
    assert _UNRELATED_TERM not in candidates.entities


def test_recall_ontology_candidates_relation_matches_on_any_component():
    # query 里完全没提"产品"，但"订单号 --BELONG_TO--> 产品"这条三元组因为
    # subject_term_type="订单号"跟query有重叠，也应该被召回（不要求三元组
    # 三个组成部分都命中才算相关）。
    candidates = recall_ontology_candidates(
        "订单号有多少个",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert AllowedCombination(
        subject_term_type="订单号", relation_type="BELONG_TO", object_term_type="产品",
    ) in candidates.relations


def test_recall_ontology_candidates_finds_field_names():
    candidates = recall_ontology_candidates(
        "price大于100的产品",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=[],
    )

    assert ("产品", "price") in candidates.fields


def test_recall_ontology_candidates_truncates_to_top_k():
    many_terms = [
        Term(tenant_id="demo", node_key=f"产品:Cola{i}", standard_name=f"Cola{i}", aliases=[], term_type="产品")
        for i in range(50)
    ]
    candidates = recall_ontology_candidates(
        "cola", terms=many_terms, term_type_schema={}, allowed_combinations=[],
    )

    assert len(candidates.entities) <= 20


def test_recall_ontology_candidates_no_match_returns_empty_lists():
    candidates = recall_ontology_candidates(
        "完全不相关的问题内容",
        terms=[_COLA_TERM],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert candidates.term_types == []
    assert candidates.relations == []
    assert candidates.fields == []
    assert candidates.entities == []


def test_format_recall_candidates_includes_relation_direction():
    candidates = recall_ontology_candidates(
        "订单号属于哪个产品",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    text = format_recall_candidates(candidates)
    assert "订单号 --BELONG_TO--> 产品" in text


def test_format_recall_candidates_empty_returns_placeholder_text():
    from app.graphrag.ontology_recall import RecallCandidates

    text = format_recall_candidates(RecallCandidates(term_types=[], relations=[], fields=[], entities=[]))
    assert text
    assert "候选" in text or "谨慎" in text


def test_recall_ontology_candidates_recalls_long_verbatim_entity_name():
    # 15个汉字，n-gram 打分法（受限于 n-gram 最长4个 token）在这个修复前
    # 永远算不出及格分数（4/15 < 0.3），即使这个名字原样出现在 query 里。
    long_name_term = Term(
        tenant_id="demo", node_key="公司:上海可口可乐饮料有限公司分公司",
        standard_name="上海可口可乐饮料有限公司分公司", aliases=[], term_type="公司",
    )
    candidates = recall_ontology_candidates(
        "上海可口可乐饮料有限公司分公司有多少个订单",
        terms=[long_name_term], term_type_schema={}, allowed_combinations=[],
    )

    assert long_name_term in candidates.entities


def test_recall_ontology_candidates_handles_large_entity_pool_quickly():
    # 5000个候选名字跟 query 没有任何字符重叠（纯 ASCII/数字 SKU 编号 vs
    # 纯中文 query），是 bigram 预过滤真正要处理的场景：绝大多数候选
    # 应该被 O(1) 的 isdisjoint 检查直接跳过，不进入昂贵的双重循环。
    # （如果候选名字反而都是 query 的近似子串——比如全部共享同一个
    # 中文前缀——bigram 预过滤反而一个都过滤不掉，测的是最坏情形，
    # 不是这个优化真正要解决的"大池子里大多数候选无关"场景。）
    import time

    target_term = Term(
        tenant_id="demo", node_key="公司:上海分公司",
        standard_name="上海分公司", aliases=[], term_type="公司",
    )
    unrelated_terms = [
        Term(tenant_id="demo", node_key=f"SKU:SKU{i:06d}", standard_name=f"SKU{i:06d}",
             aliases=[], term_type="SKU")
        for i in range(5000)
    ]
    start = time.perf_counter()
    candidates = recall_ontology_candidates(
        "上海分公司的可乐订单一共有多少条",
        terms=[target_term, *unrelated_terms], term_type_schema={}, allowed_combinations=[],
    )
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, f"recall took {elapsed:.2f}s over 5000 unrelated terms, expected well under 2s"
    assert target_term in candidates.entities
