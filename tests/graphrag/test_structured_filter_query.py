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


def test_parse_relation_constraint_with_one_hop():
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


def test_parse_relation_constraint_with_genuine_two_hops():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [
                {"relation_type": "HAS_VARIANT_GROUP", "direction": "outgoing", "target_term_type": "VariantGroup"},
                {"relation_type": "HAS_VARIANT_VALUE", "direction": "outgoing", "target_term_type": "VariantValue"},
            ],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    constraint = args.constraints[0]
    assert isinstance(constraint, RelationConstraint)
    assert constraint.hops == [
        Hop(relation_type="HAS_VARIANT_GROUP", direction="outgoing", target_term_type="VariantGroup"),
        Hop(relation_type="HAS_VARIANT_VALUE", direction="outgoing", target_term_type="VariantValue"),
    ]


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


def test_validate_rejects_relation_type_failing_name_pattern():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "has-variant", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            # 关系类型已经"被确认"，隔离出格式校验这一条失败分支，不是成员校验分支
            args, confirmed_relation_types={"has-variant"},
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )


_SKU_SCHEMA_WITH_MORE_FIELDS = TermTypeCategory(
    value="SKU",
    extra_fields=[
        ExtraFieldSpec(name="numeric_value", value_type="number"),
        ExtraFieldSpec(name="stock_count", value_type="integer"),
        ExtraFieldSpec(name="capacities", value_type="number[]"),
    ],
    node_key_template="",
)


def test_validate_accepts_integer_field_with_matching_operator():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "stock_count", "operator": "gte", "value": 10}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA_WITH_MORE_FIELDS},
    )


def test_validate_accepts_array_field_with_array_operator():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "capacities", "operator": "all_lte", "value": 500}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA_WITH_MORE_FIELDS},
    )


def test_parse_rejects_group_by_constraint_index_out_of_bounds():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
            "group_by": {"constraint_index": 5},
        })


def test_parse_rejects_group_by_constraint_index_negative():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
            "group_by": {"constraint_index": -1},
        })


def test_parse_rejects_non_string_operator():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": ["gt"], "value": 500}],
        })


def test_parse_rejects_non_int_group_by_constraint_index():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
            "group_by": {"constraint_index": "0"},
        })


def test_validate_rejects_non_string_relation_type():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": 123, "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types={"HAS_VARIANT"},
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )


def test_validate_rejects_non_string_anchor_term_type():
    args = parse_structured_filter_query_args({
        "anchor_term_type": ["SKU"],
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_parse_rejects_non_dict_constraint_element():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": ["not-a-dict"],
        })


def test_parse_rejects_non_dict_hop_element():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{
                "kind": "relation",
                "hops": ["not-a-dict"],
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
        })


def test_parse_rejects_non_dict_group_by():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
            "group_by": ["constraint_index", 0],
        })


def test_parse_rejects_non_dict_top_level_raw():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args("not-a-dict")


def test_parse_rejects_non_list_constraints():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": 5,
        })


def test_parse_rejects_non_list_hops():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor_term_type": "SKU",
            "constraints": [{
                "kind": "relation",
                "hops": 5,
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
        })


class _FakeGraphClient:
    def __init__(self, *, rows=None, group_result=None) -> None:
        self._rows = rows if rows is not None else []
        self._group_result = group_result
        self.last_args = None
        self.last_tenant_id = None

    async def execute_structured_filter_query(self, args, *, tenant_id):
        self.last_args = args
        self.last_tenant_id = tenant_id
        if self._group_result is not None:
            return self._group_result
        return self._rows


async def test_run_structured_filter_query_returns_error_on_invalid_args():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU", "constraints": []},
        graph_client=_FakeGraphClient(), tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "error" in result


async def test_run_structured_filter_query_returns_error_on_unconfirmed_field():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}]},
        graph_client=_FakeGraphClient(), tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "error" in result


async def test_run_structured_filter_query_formats_matched_results():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
         "product_line": "MUJI", "all_properties": {
             "tenant_id": "muji", "node_key": "SKU:1", "standard_name": "圆角收纳盒 500ml",
             "type": "SKU", "product_line": "MUJI", "numeric_value": 600,
         }},
    ])

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result["matched_count"] == 1
    assert result["results"] == [{
        "standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1",
        "term_type": "SKU", "product_line": "MUJI",
        "extra_properties": {"numeric_value": 600},
    }]
    assert graph_client.last_tenant_id == "muji"


async def test_run_structured_filter_query_passes_through_group_by_result():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(group_result={"groups": [{"value": "红色", "count": 12}]})

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{
             "kind": "relation",
             "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
             "target_field": "raw_value", "target_operator": "eq", "target_value": "__group__",
         }],
         "group_by": {"constraint_index": 0}},
        graph_client=graph_client, tenant_id="muji",
        confirmed_relation_types={"HAS_VARIANT"},
        term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
    )

    assert result == {"groups": [{"value": "红色", "count": 12}]}
