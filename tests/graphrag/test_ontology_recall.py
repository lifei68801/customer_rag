from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination
from app.graphrag.ontology_recall import (
    RecallPath,
    RecallPathHop,
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

    text = format_recall_candidates(
        RecallCandidates(term_types=[], relations=[], fields=[], paths=[], entities=[])
    )
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


def test_recall_ontology_candidates_finds_two_hop_path_across_intermediate_type():
    # "coke-cola公司有多少个订单"这类问题里，"订单号"和"公司"之间没有直接
    # 关系，必须经过"产品"这个中间类型才能连起来——回归 2026-08-27 排查
    # 到的真实案例：深层参数生成 LLM 没能自己推理出这条两跳链路，导致
    # anchor.term_type="订单号" 却没带任何公司过滤，把全部订单数当成了
    # 答案。这里钉住"召回结果必须包含这条完整路径"这个前提条件。
    candidates = recall_ontology_candidates(
        "coke-cola公司有多少个订单",
        terms=[_COCA_COLA_TERM],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert RecallPath(
        source_term_type="订单号",
        hops=(
            RecallPathHop(relation_type="BELONG_TO", direction="outgoing", target_term_type="产品"),
            RecallPathHop(relation_type="BELONG_TO", direction="outgoing", target_term_type="公司"),
        ),
    ) in candidates.paths


def test_recall_ontology_candidates_finds_reverse_direction_path():
    # 反过来从"公司"出发找"订单号"，两跳都要沿关系反方向走
    # （incoming）——BFS 必须双向都能走，不能只支持 subject->object 这一个
    # 方向。
    candidates = recall_ontology_candidates(
        "公司名下的订单号",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert RecallPath(
        source_term_type="公司",
        hops=(
            RecallPathHop(relation_type="BELONG_TO", direction="incoming", target_term_type="产品"),
            RecallPathHop(relation_type="BELONG_TO", direction="incoming", target_term_type="订单号"),
        ),
    ) in candidates.paths


def test_recall_ontology_candidates_paths_exclude_single_hop_reachable_targets():
    # "产品"从"订单号"出发只需要 1 跳就能到，1 跳关系已经由 candidates.relations
    # 单独覆盖（见 test_recall_ontology_candidates_relation_matches_on_any_component）
    # ——不应该在 paths 里重复出现一条同样只有 1 跳的"路径"。
    candidates = recall_ontology_candidates(
        "订单号属于哪个产品",
        terms=[],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    assert all(len(path.hops) >= 2 for path in candidates.paths)


def test_format_recall_candidates_renders_multi_hop_path_with_arrows():
    candidates = recall_ontology_candidates(
        "coke-cola公司有多少个订单",
        terms=[_COCA_COLA_TERM],
        term_type_schema=_TERM_TYPE_SCHEMA,
        allowed_combinations=_ALLOWED_COMBINATIONS,
    )

    text = format_recall_candidates(candidates)

    assert "订单号 --BELONG_TO--> 产品 --BELONG_TO--> 公司" in text


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


def test_fbeta_match_score_ranks_full_company_name_above_short_product_name():
    # 2026-08-27 真实 bug 的核心成因：`Cola`(4字) 是 `Coca-Cola`(9字) 的子串，
    # 任何单向的"匹配长度/len(name)"公式，Cola 都拿满分 1.0 碾压 Coca-Cola，
    # 导致召回把错误的产品实体排在正确的公司实体前面。F-beta 的 R 项
    # （匹配长度/len(query)）惩罚"query 很长却只匹配上一小段"的短候选名，
    # 把这个排序扭转过来。
    from app.graphrag.ontology_recall import fbeta_match_score

    query = "coke-cola公司有多少个订单"
    assert fbeta_match_score(query, "Coca-Cola") > fbeta_match_score(query, "Cola")


def test_fbeta_match_score_gives_zero_for_unrelated_name():
    from app.graphrag.ontology_recall import fbeta_match_score

    assert fbeta_match_score("coke-cola公司有多少个订单", "服务器连接超时") == 0.0


def test_fbeta_match_score_treats_whole_name_containment_as_full_match():
    # 候选名整段出现在 query 里时按满额匹配算（保留现有 _best_score 的
    # containment 快速路径语义），此时 precision 应该是 1.0，最终分只受
    # recall 项影响、不应该被间隔约束打折。
    from app.graphrag.ontology_recall import fbeta_match_score

    assert fbeta_match_score("我想查 Coca-Cola 的订单", "Coca-Cola") > 0.5


def test_entity_recall_survives_a_long_query():
    # 回归 2026-08-28 final review 的 Critical：_MIN_SCORE 是绝对及格线，
    # 而 fbeta 的 recall 项带 len(query)——拿 fbeta 当及格线判据时，同一个
    # 实体会因为 query 变长而整个消失。实测整段命中下 len(query) 超过
    # 12.67*len(name) 就跌破 0.3，而生产调用方拼出的 query_text 常有
    # 60-120 字符。gate 必须用 precision（跟 query 长度无关）。
    long_query = (
        "查询可口可乐这家公司名下的订单数量，可口可乐是我们的重点客户，"
        "需要重点关注它今年的整体经营情况和后续合作计划"
    )
    coke = Term(tenant_id="demo", node_key="公司:可口可乐",
                standard_name="可口可乐", aliases=[], term_type="公司")
    candidates = recall_ontology_candidates(
        long_query, terms=[coke], term_type_schema={}, allowed_combinations=[],
    )
    assert coke in candidates.entities


def test_entity_recall_rejects_coincidental_short_matches():
    # 回归 2026-08-28 final-fix 轮次发现的问题：把实体 gate 放在 _MIN_SCORE(0.3)
    # 上时，无关人名靠在 "coca-cola" 里凑一两个字符就能进候选——实测
    # Alice 0.4000、Paul Cole 0.4444。0.3 这个值是给旧的连续子串打分器定的，
    # 对子序列打分太松，所以实体单独用 _ENTITY_MIN_SCORE。
    query = "查询Coca-Cola这家公司名下有多少个订单"
    coke = Term(tenant_id="demo", node_key="公司:Coca-Cola",
                standard_name="Coca-Cola", aliases=[], term_type="公司")
    alice = Term(tenant_id="demo", node_key="用户名:Alice",
                 standard_name="Alice", aliases=[], term_type="用户名")

    candidates = recall_ontology_candidates(
        query, terms=[coke, alice], term_type_schema={}, allowed_combinations=[],
    )

    assert coke in candidates.entities
    assert alice not in candidates.entities


def test_character_set_prefilter_does_not_drop_a_fully_matched_candidate():
    # 回归 final review 的 Important：旧的 bigram 预过滤是给"最长连续公共
    # 子串 + 最小重叠2字符"设计的，对间隔约束子序列打分不成立。
    # "订购单据编号是多少" 和 "订单号" 的 bigram 交集为空，但 订/单/号
    # 按顺序都在、间隔都没超限，真实 precision 是 1.0，不该被跳过。
    from app.graphrag.ontology_recall import precision_match_score

    assert precision_match_score("订购单据编号是多少", "订单号") == 1.0

    candidates = recall_ontology_candidates(
        "订购单据编号是多少",
        terms=[],
        term_type_schema={"订单号": TermTypeCategory(value="订单号", extra_fields=[])},
        allowed_combinations=[],
    )
    assert "订单号" in candidates.term_types


def test_short_term_type_labels_are_not_penalised_by_query_length():
    # 本体词汇（term_type/relation/field）的名字天生就短，不能套用实体名那套
    # F-beta——recall 项会因为 query 长就把"订单号"这类正确标签打到及格线以下。
    # 2026-08-28 实测：F-beta 下"订单号"对这句 query 得 0.2857 < _MIN_SCORE(0.3)，
    # 而它显然是相关类型；precision-only 得 0.6667。
    from app.graphrag.ontology_recall import _MIN_SCORE as _MIN_SCORE_FOR_TEST
    from app.graphrag.ontology_recall import fbeta_match_score, precision_match_score

    query = "查询Coca-Cola这家公司名下有多少个订单"
    assert precision_match_score(query, "订单号") >= _MIN_SCORE_FOR_TEST
    assert fbeta_match_score(query, "订单号") < _MIN_SCORE_FOR_TEST
