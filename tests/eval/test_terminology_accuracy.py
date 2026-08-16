from app.eval.terminology_accuracy import score_terminology_accuracy
from app.graphrag.ontology import Term

_TERMS = [
    Term(
        tenant_id="t1",
        node_key="示例错误码E502",
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
        product_line="示例产品线",
    ),
]


def test_scores_one_when_answer_uses_standard_name():
    score = score_terminology_accuracy(
        "由于示例错误码E502，请检查网关设置", _TERMS
    )

    assert score == 1.0


def test_scores_zero_when_answer_uses_only_alias():
    score = score_terminology_accuracy("由于网关超时示例，请检查网关设置", _TERMS)

    assert score == 0.0


def test_returns_none_when_no_relevant_terminology_mentioned():
    score = score_terminology_accuracy("今天天气不错", _TERMS)

    assert score is None
