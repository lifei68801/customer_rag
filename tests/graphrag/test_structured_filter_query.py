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
)
_VARIANT_SCHEMA = TermTypeCategory(
    value="VariantValue", extra_fields=[ExtraFieldSpec(name="raw_value", value_type="string")],
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


_SKU_SCHEMA_WITH_LEGACY_UNSAFE_FIELD = TermTypeCategory(
    value="SKU",
    extra_fields=[
        ExtraFieldSpec(name="numeric_value", value_type="number"),
        # 模拟 _migrate_extra_fields_value_shape_if_needed 迁移出的历史遗留字段——
        # 这里直接构造 dataclass，绕过 ontology_categories.py::create_term_type 会走的
        # _validate_extra_field_specs 声明期格式校验，等价于该函数描述的迁移期绕过场景。
        ExtraFieldSpec(name='bad field"}) DETACH DELETE (n', value_type="string"),
    ],
)


def test_validate_rejects_legacy_unsafe_field_name_bypassing_declaration_time_validation():
    """字段名匹配成功（是已知字段）不代表可以安全拼进 Cypher 文本——
    _resolve_field_value_type 必须在返回前对 spec.name 做防御性格式复检，
    否则历史遗留字段（未经 _validate_extra_field_specs 校验）会被当作安全字段放行。"""
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "attribute", "field": 'bad field"}) DETACH DELETE (n', "operator": "eq", "value": "x",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, confirmed_relation_types=set(),
            term_type_schema={"SKU": _SKU_SCHEMA_WITH_LEGACY_UNSAFE_FIELD},
        )


def test_validate_still_accepts_normal_field_when_schema_also_has_legacy_unsafe_field():
    """确认防御性校验不误伤：schema 里混有历史遗留不安全字段时，正常声明的字段
    依然应该照常通过——不能因为加了这层复检就把整个 term_type 判成不可用。"""
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    validate_structured_filter_query(
        args, confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA_WITH_LEGACY_UNSAFE_FIELD},
    )  # 不抛异常即通过


def test_validate_error_on_unknown_field_lists_available_fields():
    """校验失败的消息必须把"什么才是对的"一并告诉 LLM——工具调用轮次通常只有 3 轮
    预算，只说"你写错了"而不说对的是什么，LLM 没有任何信息可以自我纠正。"""
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        validate_structured_filter_query(
            args, confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )
    message = str(exc_info.value)
    assert "可用字段:" in message
    assert "numeric_value" in message
    assert "standard_name" in message


def test_validate_error_on_unknown_anchor_term_type_lists_available_term_types():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "NotAType",
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        validate_structured_filter_query(
            args, confirmed_relation_types=set(),
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )
    message = str(exc_info.value)
    assert "可用的 term_type:" in message
    assert "SKU" in message
    assert "VariantValue" in message


def test_validate_error_on_unconfirmed_relation_type_lists_available_relation_types():
    args = parse_structured_filter_query_args({
        "anchor_term_type": "SKU",
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "NOT_CONFIRMED", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        validate_structured_filter_query(
            args, confirmed_relation_types={"HAS_VARIANT", "BELONGS_TO"},
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )
    message = str(exc_info.value)
    assert "可用的 relation_type:" in message
    assert "HAS_VARIANT" in message
    assert "BELONGS_TO" in message


class _FakeGraphClient:
    def __init__(self, *, rows=None, group_result=None, error=None) -> None:
        self._rows = rows if rows is not None else []
        self._group_result = group_result
        self._error = error
        self.last_args = None
        self.last_tenant_id = None

    async def execute_structured_filter_query(self, args, *, tenant_id):
        self.last_args = args
        self.last_tenant_id = tenant_id
        if self._error is not None:
            raise self._error
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


async def test_run_structured_filter_query_returns_error_when_graph_execution_raises():
    """图谱后端异常（驱动挂了、Cypher 运行时类型错误等）必须降级成这一次工具调用的
    错误观察结果——异常穿过 planner.py::run_tool_calls 的 asyncio.gather 会被重新抛出，
    把整个 Agent SSE 回合打挂，而不是只损失一次工具调用。"""
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(error=RuntimeError("driver error"))

    result = await run_structured_filter_query(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji",
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result == {"error": "图谱查询执行失败：driver error"}
