from app.graphrag.ontology import Term
from app.graphrag.question_validation import find_unmatched_questions


def _term(name: str, term_type: str) -> Term:
    """Term 的字段已核对（app/graphrag/ontology.py:10-17）：
    tenant_id / node_key / standard_name / aliases / term_type /
    extra_properties（默认空 dict）/ source（默认 'unknown'）。"""
    return Term(
        tenant_id="t1",
        node_key=f"{term_type}:{name}",
        standard_name=name,
        aliases=[],
        term_type=term_type,
    )


def test_a_question_naming_a_known_entity_passes():
    terms = [_term("Beer", "产品"), _term("柚子", "口味")]
    assert find_unmatched_questions(["Beer 是什么口味的？"], terms) == []


def test_a_question_naming_a_known_type_passes():
    """问「产品有哪些口味」时，「产品」和「口味」都是类型名不是实体名。
    只认实体名的话，本体里最典型的那类问题会被判成不可答。"""
    terms = [_term("Beer", "产品"), _term("柚子", "口味")]
    assert find_unmatched_questions(["产品有哪些口味？"], terms) == []


def test_a_question_naming_nothing_in_the_ontology_is_reported():
    """「库存多少？」在一个没有库存概念的本体里必须被报出来。
    这是这个函数存在的理由。"""
    terms = [_term("Beer", "产品")]
    assert find_unmatched_questions(["库存多少？"], terms) == ["库存多少？"]


def test_only_the_unmatched_ones_are_returned():
    """批次里同时有能匹配和不能匹配的。全都能匹配的批次下，
    「一律返回空」的实现也能变绿。"""
    terms = [_term("Beer", "产品")]
    assert find_unmatched_questions(
        ["Beer 是什么？", "库存多少？", "产品有哪些？"], terms
    ) == ["库存多少？"]


def test_an_empty_ontology_reports_every_question():
    """本体是空的时候，任何手写问题都答不出来。此时全部报出来而不是
    全部放行——放行的话，新租户配的问题一条都点不动，而保存时什么都没说。"""
    assert find_unmatched_questions(["随便什么"], []) == ["随便什么"]


def test_matching_ignores_case_and_surrounding_punctuation():
    """「beer 是什么？」和「Beer 是什么？」是同一个问题。
    大小写敏感的匹配会让审核员反复困惑于为什么保存不了。"""
    terms = [_term("Beer", "产品")]
    assert find_unmatched_questions(["beer 是什么？"], terms) == []
