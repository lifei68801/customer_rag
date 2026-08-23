from pathlib import Path

from app.graphrag.ontology import Term, find_candidate_term_types, load_terminology, resolve_term


def _term(standard_name: str, term_type: str, *, node_key: str | None = None, aliases: list[str] | None = None) -> Term:
    return Term(
        tenant_id="t1",
        node_key=node_key or f"{term_type}:{standard_name}",
        standard_name=standard_name,
        aliases=aliases or [],
        term_type=term_type,
    )


def test_resolve_term_matches_exact_type():
    terms = [_term("Coffee", "产品"), _term("Coffee", "类目")]

    result = resolve_term(terms=terms, name="Coffee", term_type_hint="类目")

    assert result is not None
    assert result.term_type == "类目"


def test_resolve_term_matches_via_alias_within_hinted_type():
    terms = [
        _term("拿铁", "产品", aliases=["Latte"]),
        _term("拿铁咖啡杯", "商品", aliases=[]),
    ]

    result = resolve_term(terms=terms, name="Latte", term_type_hint="产品")

    assert result is not None
    assert result.standard_name == "拿铁"


def test_resolve_term_falls_back_when_name_is_unambiguous():
    terms = [_term("拿铁", "产品")]

    result = resolve_term(terms=terms, name="拿铁", term_type_hint=None)

    assert result is not None
    assert result.standard_name == "拿铁"


def test_resolve_term_falls_back_via_alias_when_unambiguous():
    terms = [_term("拿铁", "产品", aliases=["Latte"])]

    result = resolve_term(terms=terms, name="Latte", term_type_hint=None)

    assert result is not None
    assert result.standard_name == "拿铁"


def test_resolve_term_falls_back_when_hint_type_has_no_match_but_name_is_unambiguous():
    terms = [_term("拿铁", "产品")]

    result = resolve_term(terms=terms, name="拿铁", term_type_hint="类目")

    assert result is not None
    assert result.term_type == "产品"


def test_resolve_term_returns_none_when_hinted_type_has_alias_collision():
    """同一个 term_type 下出现两条术语共享同一个名字/别名（DB 唯一索引只
    覆盖 (tenant_id, term_type, standard_name)，不覆盖 aliases——JSON 列，
    且 ETL upsert 路径 upsert_term_with_node_key 不像 create_term/
    update_term 那样跑 _check_name_conflict 别名冲突检查，所以这种脏数据
    是可能出现的）：命中该类型的不是一条而是两条，即使传了 term_type_hint
    也必须返回 None，不能因为"传了 hint 就直接返回第一条命中的"而随便
    选中其中一个——这是本次改动收紧的行为。"""
    terms = [
        _term("拿铁", "产品", node_key="产品:拿铁", aliases=["Latte"]),
        _term("Latte", "产品", node_key="产品:Latte"),
    ]

    result = resolve_term(terms=terms, name="Latte", term_type_hint="产品")

    assert result is None


def test_resolve_term_returns_none_when_ambiguous_without_hint():
    terms = [_term("Coffee", "产品"), _term("Coffee", "类目")]

    result = resolve_term(terms=terms, name="Coffee", term_type_hint=None)

    assert result is None


def test_resolve_term_returns_none_when_not_found_at_all():
    terms = [_term("拿铁", "产品")]

    result = resolve_term(terms=terms, name="不存在", term_type_hint=None)

    assert result is None


def test_find_candidate_term_types_returns_empty_list_when_not_found():
    terms = [_term("拿铁", "产品")]

    assert find_candidate_term_types("不存在", terms) == []


def test_find_candidate_term_types_returns_sorted_types_when_matched_by_name_or_alias():
    terms = [_term("Coffee", "产品"), _term("Coffee", "类目")]

    assert find_candidate_term_types("Coffee", terms) == sorted(["产品", "类目"])


def test_find_candidate_term_types_matches_via_alias():
    terms = [_term("拿铁", "产品", aliases=["Latte"])]

    assert find_candidate_term_types("Latte", terms) == ["产品"]


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
