from pathlib import Path

from app.graphrag.ontology import Term, find_term_by_type_hint, load_terminology


def _term(standard_name: str, term_type: str, *, node_key: str | None = None) -> Term:
    return Term(
        tenant_id="t1",
        node_key=node_key or f"{term_type}:{standard_name}",
        standard_name=standard_name,
        aliases=[],
        term_type=term_type,
    )


def test_find_term_by_type_hint_matches_exact_type():
    terms = [_term("Coffee", "产品"), _term("Coffee", "类目")]

    result = find_term_by_type_hint(terms, "Coffee", term_type_hint="类目")

    assert result is not None
    assert result.term_type == "类目"


def test_find_term_by_type_hint_falls_back_when_name_is_unambiguous():
    terms = [_term("拿铁", "产品")]

    result = find_term_by_type_hint(terms, "拿铁", term_type_hint=None)

    assert result is not None
    assert result.standard_name == "拿铁"


def test_find_term_by_type_hint_falls_back_when_hint_type_has_no_match_but_name_is_unambiguous():
    terms = [_term("拿铁", "产品")]

    result = find_term_by_type_hint(terms, "拿铁", term_type_hint="类目")

    assert result is not None
    assert result.term_type == "产品"


def test_find_term_by_type_hint_returns_none_when_ambiguous_without_hint():
    terms = [_term("Coffee", "产品"), _term("Coffee", "类目")]

    result = find_term_by_type_hint(terms, "Coffee", term_type_hint=None)

    assert result is None


def test_find_term_by_type_hint_returns_none_when_not_found_at_all():
    terms = [_term("拿铁", "产品")]

    result = find_term_by_type_hint(terms, "不存在", term_type_hint=None)

    assert result is None


def test_load_terminology_parses_terms_with_aliases(tmp_path):
    yaml_path = tmp_path / "terminology.yaml"
    yaml_path.write_text(
        "terms:\n"
        "  - standard_name: 错误码E502\n"
        "    aliases: [网关超时, E502]\n"
        "    term_type: error_code\n",
        encoding="utf-8",
    )

    terms = load_terminology(yaml_path)

    assert terms == [
        Term(
            tenant_id="default",
            node_key="错误码E502",
            standard_name="错误码E502",
            aliases=["网关超时", "E502"],
            term_type="error_code",
        )
    ]
