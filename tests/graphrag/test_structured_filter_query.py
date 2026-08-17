from __future__ import annotations

import pytest

from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.structured_filter_query import (
    AttributeConstraint,
    GroupBy,
    Hop,
    RelationConstraint,
    StructuredFilterQueryError,
    parse_structured_filter_query_args,
    validate_structured_filter_query,
)


def test_parse_attribute_constraint():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    assert args.anchor_term_type == "SKU"
    assert args.constraints == [AttributeConstraint(field="numeric_value", operator="gt", value=500)]
    assert args.group_by is None
    assert args.limit == 20


def test_parse_relation_constraint_with_two_hops():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [
                {"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"},
            ],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    constraint = args.constraints[0]
    assert isinstance(constraint, RelationConstraint)
    assert constraint.hops == [Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")]
    assert constraint.target_field == "raw_value"


def test_parse_rejects_empty_constraints():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor_term_type": "SKU", "constraints": []})


def test_parse_rejects_more_than_two_hops():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{
                "kind": "relation",
                "hops": [
                    {"relation_type": "A", "direction": "outgoing", "target_term_type": "X"},
                    {"relation_type": "B", "direction": "outgoing", "target_term_type": "Y"},
                    {"relation_type": "C", "direction": "outgoing", "target_term_type": "Z"},
                ],
                "target_field": "f", "target_operator": "eq", "target_value": "v",
            }],
        })


def test_parse_rejects_unknown_operator():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{"kind": "attribute", "field": "x", "operator": "contains", "value": "y"}],
        })


def test_parse_group_by_must_point_to_relation_constraint():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
            "group_by": {"constraint_index": 0},
        })


def test_parse_uses_default_limit_when_omitted():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    assert args.limit == 20


_SKU_SCHEMA = TermTypeCategory(
    value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    node_key_template="",
)
_VARIANT_SCHEMA = TermTypeCategory(
    value="VariantValue", extra_fields=[ExtraFieldSpec(name="raw_value", value_type="string")],
    node_key_template="",
)


def test_validate_accepts_confirmed_field_and_matching_operator():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA},
    )  # 不抛异常即通过


def test_validate_rejects_field_not_in_confirmed_schema():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_rejects_operator_not_matching_declared_value_type():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "starts_with", "value": "5"}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_accepts_standard_name_as_reserved_field():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "starts_with", "value": "圆角"}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )


def test_validate_rejects_relation_type_not_confirmed():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(),  # 空集合，HAS_VARIANT 未确认
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )


def test_validate_accepts_confirmed_relation_type_and_target_field():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types={"HAS_VARIANT"},
        term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
    )
