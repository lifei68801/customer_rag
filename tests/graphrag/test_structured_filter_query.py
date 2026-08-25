from __future__ import annotations

import pytest

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.structured_filter_query import (
    AttributeConstraint,
    ExpandSpec,
    GroupBy,
    Hop,
    NameAnchor,
    RelationConstraint,
    ResolvedAnchor,
    StructuredFilterQueryError,
    TypeAnchor,
    parse_structured_filter_query_args,
    run_structured_filter_query,
    validate_structured_filter_query,
)


def test_parse_attribute_constraint():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    assert args.anchor == TypeAnchor(term_type="SKU")
    assert args.constraints == [AttributeConstraint(field="numeric_value", operator="gt", value=500)]
    assert args.expand is None
    assert args.group_by is None
    assert args.limit == 20


def test_parse_relation_constraint_with_one_hop():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
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
        "anchor": {"term_type": "SKU"},
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
        parse_structured_filter_query_args({"anchor": {"term_type": "SKU"}, "constraints": []})


def test_parse_rejects_more_than_two_hops():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
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
            "anchor": {"term_type": "SKU"},
            "constraints": [{"kind": "attribute", "field": "x", "operator": "contains", "value": "y"}],
        })


def test_parse_group_by_must_point_to_relation_constraint():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
            "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
            "group_by": {"constraint_index": 0},
        })


def test_parse_uses_default_limit_when_omitted():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
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
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )  # 不抛异常即通过


def test_validate_rejects_field_not_in_confirmed_schema():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_rejects_operator_not_matching_declared_value_type():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "starts_with", "value": "5"}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_accepts_standard_name_as_reserved_field():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "starts_with", "value": "圆角"}],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )


def test_validate_rejects_relation_type_not_confirmed():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types=set(),  # 空集合，HAS_VARIANT 未确认
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )


def test_validate_accepts_confirmed_relation_type_and_target_field():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        confirmed_relation_types={"HAS_VARIANT"},
        term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
    )


def test_validate_rejects_relation_type_failing_name_pattern():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "has-variant", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            # 关系类型已经"被确认"，隔离出格式校验这一条失败分支，不是成员校验分支
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types={"has-variant"},
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
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "stock_count", "operator": "gte", "value": 10}],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA_WITH_MORE_FIELDS},
    )


def test_validate_accepts_array_field_with_array_operator():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "capacities", "operator": "all_lte", "value": 500}],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA_WITH_MORE_FIELDS},
    )


def test_parse_rejects_group_by_constraint_index_out_of_bounds():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
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
            "anchor": {"term_type": "SKU"},
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
            "anchor": {"term_type": "SKU"},
            "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": ["gt"], "value": 500}],
        })


def test_parse_rejects_non_int_group_by_constraint_index():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
            "group_by": {"constraint_index": "0"},
        })


def test_validate_rejects_non_string_relation_type():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": 123, "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types={"HAS_VARIANT"},
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )


def test_validate_rejects_non_string_anchor_term_type():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type=["SKU"], node_key=None),
            confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_parse_rejects_non_dict_constraint_element():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
            "constraints": ["not-a-dict"],
        })


def test_parse_rejects_non_dict_hop_element():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
            "constraints": [{
                "kind": "relation",
                "hops": ["not-a-dict"],
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
        })


def test_parse_rejects_non_dict_group_by():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
            "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
            "group_by": ["constraint_index", 0],
        })


def test_parse_rejects_expand_combined_with_group_by():
    """expand 和 group_by 不能同时使用：group_by 走聚合分支，会在 neo4j_client.py 里
    直接返回聚合结果、根本不构建 expand 子句——同时传两者时旧行为是 group_by 静默
    获胜、expand 被无声丢弃，这里改成在解析层就直接报错，让 LLM 看到明确的拒绝原因。"""
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
                "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
            }],
            "group_by": {"constraint_index": 0},
            "expand": {"hops": 1},
        })


def test_parse_expand_alone_still_works_without_group_by():
    """确认 expand 单独使用不受上面那条新校验影响。"""
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {"hops": 1},
    })
    assert args.expand == ExpandSpec(hops=1, relation_type=None, direction="both")
    assert args.group_by is None


def test_parse_group_by_alone_still_works_without_expand():
    """确认 group_by 单独使用不受上面那条新校验影响。"""
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
        "group_by": {"constraint_index": 0},
    })
    assert args.group_by == GroupBy(constraint_index=0)
    assert args.expand is None


def test_parse_rejects_non_dict_top_level_raw():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args("not-a-dict")


def test_parse_rejects_non_list_constraints():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
            "constraints": 5,
        })


def test_parse_rejects_non_list_hops():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"},
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
        "anchor": {"term_type": "SKU"},
        "constraints": [{
            "kind": "attribute", "field": 'bad field"}) DETACH DELETE (n', "operator": "eq", "value": "x",
        }],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types=set(),
            term_type_schema={"SKU": _SKU_SCHEMA_WITH_LEGACY_UNSAFE_FIELD},
        )


def test_validate_still_accepts_normal_field_when_schema_also_has_legacy_unsafe_field():
    """确认防御性校验不误伤：schema 里混有历史遗留不安全字段时，正常声明的字段
    依然应该照常通过——不能因为加了这层复检就把整个 term_type 判成不可用。"""
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        confirmed_relation_types=set(),
        term_type_schema={"SKU": _SKU_SCHEMA_WITH_LEGACY_UNSAFE_FIELD},
    )  # 不抛异常即通过


def test_validate_error_on_unknown_field_lists_available_fields():
    """校验失败的消息必须把"什么才是对的"一并告诉 LLM——工具调用轮次通常只有 3 轮
    预算，只说"你写错了"而不说对的是什么，LLM 没有任何信息可以自我纠正。"""
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )
    message = str(exc_info.value)
    assert "可用字段:" in message
    assert "numeric_value" in message
    assert "standard_name" in message


def test_validate_error_on_unknown_anchor_term_type_lists_available_term_types():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "NotAType"},
        "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}],
    })
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="NotAType", node_key=None),
            confirmed_relation_types=set(),
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )
    message = str(exc_info.value)
    assert "可用的 term_type:" in message
    assert "SKU" in message
    assert "VariantValue" in message


def test_validate_error_on_unconfirmed_relation_type_lists_available_relation_types():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "NOT_CONFIRMED", "direction": "outgoing", "target_term_type": "VariantValue"}],
            "target_field": "raw_value", "target_operator": "eq", "target_value": "红",
        }],
    })
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types={"HAS_VARIANT", "BELONGS_TO"},
            term_type_schema={"SKU": _SKU_SCHEMA, "VariantValue": _VARIANT_SCHEMA},
        )
    message = str(exc_info.value)
    assert "可用的 relation_type:" in message
    assert "HAS_VARIANT" in message
    assert "BELONGS_TO" in message


class _FakeGraphClient:
    def __init__(self, *, rows=None, group_result=None, error=None, total_count=None) -> None:
        self._rows = rows if rows is not None else []
        self._group_result = group_result
        self._error = error
        self._total_count = total_count if total_count is not None else len(self._rows)
        self.last_args = None
        self.last_resolved = None
        self.last_tenant_id = None

    async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
        self.last_args = args
        self.last_resolved = resolved
        self.last_tenant_id = tenant_id
        if self._error is not None:
            raise self._error
        if self._group_result is not None:
            return self._group_result
        return {"rows": self._rows, "total_count": self._total_count}


async def test_run_structured_filter_query_returns_error_on_invalid_args():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"}, "constraints": []},
        graph_client=_FakeGraphClient(), tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "error" in result


async def test_run_structured_filter_query_returns_error_on_unconfirmed_field():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "unknown_field", "operator": "gt", "value": 500}]},
        graph_client=_FakeGraphClient(), tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "error" in result


async def test_run_structured_filter_query_formats_matched_results():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
         "all_properties": {
             "tenant_id": "muji", "node_key": "SKU:1", "standard_name": "圆角收纳盒 500ml",
             "type": "SKU", "numeric_value": 600,
         }},
    ])

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result["matched_count"] == 1
    assert result["anchors"] == [{
        "standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1",
        "term_type": "SKU",
        "extra_properties": {"numeric_value": 600},
    }]
    assert graph_client.last_tenant_id == "muji"


async def test_run_structured_filter_query_excludes_legacy_product_line_residue_from_extra_properties():
    """Neo4j 上预存在的 :Term 节点可能还残留着移除前写入的 product_line 属性（既定
    决定：不做批量迁移清理）。properties(anchor) 会原样把它读出来，混进
    all_properties——如果只用 _CORE_TERM_FIELDS 过滤，它会被当成"实体自定义属性"
    泄露进 extra_properties、进而出现在 LLM 上下文里。这里断言它被
    _LEGACY_RESIDUAL_NODE_PROPERTIES 挡住，同时真正的自定义属性不受影响。"""
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
         "all_properties": {
             "tenant_id": "muji", "node_key": "SKU:1", "standard_name": "圆角收纳盒 500ml",
             "type": "SKU", "numeric_value": 600, "product_line": "示例产品线",
         }},
    ])

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result["anchors"] == [{
        "standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1",
        "term_type": "SKU",
        "extra_properties": {"numeric_value": 600},
    }]
    extra_properties = result["anchors"][0]["extra_properties"]
    assert "product_line" not in extra_properties
    assert extra_properties["numeric_value"] == 600


async def test_run_structured_filter_query_passes_through_group_by_result():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(group_result={"groups": [{"value": "红色", "count": 12}]})

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{
             "kind": "relation",
             "hops": [{"relation_type": "HAS_VARIANT", "direction": "outgoing", "target_term_type": "VariantValue"}],
             "target_field": "raw_value", "target_operator": "eq", "target_value": "__group__",
         }],
         "group_by": {"constraint_index": 0}},
        graph_client=graph_client, tenant_id="muji", terms=[],
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
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result == {"error": "图谱查询执行失败：driver error"}


async def test_run_structured_filter_query_marks_truncated_when_total_exceeds_returned_rows():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(
        rows=[{"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
               "all_properties": {"numeric_value": 600}}],
        total_count=42,
    )

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert result["matched_count"] == 42
    assert result["truncated"] is True


async def test_run_structured_filter_query_no_truncated_flag_when_total_matches_returned_rows():
    from app.graphrag.structured_filter_query import run_structured_filter_query

    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "圆角收纳盒 500ml", "node_key": "SKU:1", "term_type": "SKU",
         "all_properties": {"numeric_value": 600}},
    ])

    result = await run_structured_filter_query(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        graph_client=graph_client, tenant_id="muji", terms=[],
        confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
    )

    assert "truncated" not in result


_COKE_TERM = Term(
    tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
    aliases=["coke-cola", "可口可乐"], term_type="公司",
)


async def test_run_structured_filter_query_resolves_name_anchor_and_uses_node_key():
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {"anchor": {"name": "coke-cola"}},
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(), term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )

    assert result["matched_count"] == 1
    assert result["anchors"][0]["standard_name"] == "Coca-Cola"
    # 锚点用解析出的 node_key 精确定位，不是按 type 扫描——通过 _FakeGraphClient
    # 记录的 last_resolved 断言 resolve_term() 解析出的 node_key 被正确传下去。
    assert graph_client.last_resolved.node_key == "公司:Coca-Cola"


async def test_run_structured_filter_query_name_anchor_not_resolved_returns_zero_without_querying_graph():
    class _ExplodingGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            raise AssertionError("未命中术语表时不应该查图谱")

    result = await run_structured_filter_query(
        {"anchor": {"name": "完全不认识的名字"}},
        graph_client=_ExplodingGraphClient(), tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(), term_type_schema={},
    )

    assert result == {"matched_count": 0, "anchors": []}


async def test_run_structured_filter_query_name_anchor_uses_type_hint_to_disambiguate():
    terms = [
        Term(tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee", aliases=[], term_type="产品"),
        Term(tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee", aliases=[], term_type="类目"),
    ]
    graph_client = _FakeGraphClient(rows=[])

    await run_structured_filter_query(
        {"anchor": {"name": "Coffee", "type_hint": "类目"}},
        graph_client=graph_client, tenant_id="t1", terms=terms,
        confirmed_relation_types=set(),
        term_type_schema={"类目": TermTypeCategory(value="类目", extra_fields=[])},
    )

    assert graph_client.last_resolved.node_key == "类目:Coffee"


_SALES_SCHEMA_NUMBER = TermTypeCategory(
    value="销量", extra_fields=[], standard_name_value_type="number",
)


def test_validate_accepts_numeric_operator_on_standard_name_when_declared_number():
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "销量"},
        "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "gt", "value": 50}],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="销量", node_key=None),
        confirmed_relation_types=set(), term_type_schema={"销量": _SALES_SCHEMA_NUMBER},
    )  # 不抛异常即通过


def test_validate_still_rejects_numeric_operator_on_standard_name_when_default_string():
    """默认 value_type='string' 的 term type，行为不能变——防回归。"""
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "SKU"},
        "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "gt", "value": 50}],
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
            confirmed_relation_types=set(), term_type_schema={"SKU": _SKU_SCHEMA},
        )


def test_validate_relation_target_field_standard_name_respects_declared_type():
    """target_field=standard_name（relation 约束的最后一跳）也要读同一份声明，
    不只是 attribute 约束的 anchor 自身。"""
    args = parse_structured_filter_query_args({
        "anchor": {"term_type": "订单号"},
        "constraints": [{
            "kind": "relation",
            "hops": [{"relation_type": "BELONG_TO", "direction": "incoming", "target_term_type": "销量"}],
            "target_field": "standard_name", "target_operator": "gt", "target_value": 50,
        }],
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="订单号", node_key=None),
        confirmed_relation_types={"BELONG_TO"},
        term_type_schema={
            "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
            "销量": _SALES_SCHEMA_NUMBER,
        },
    )  # 不抛异常即通过


def test_parse_name_anchor():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola", "type_hint": "公司"},
        "constraints": [],
    })
    assert args.anchor == NameAnchor(name="coke-cola", type_hint="公司")


def test_parse_name_anchor_without_type_hint():
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}, "constraints": []})
    assert args.anchor == NameAnchor(name="coke-cola", type_hint=None)


def test_parse_rejects_anchor_with_both_name_and_term_type():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"name": "coke-cola", "term_type": "公司"},
            "constraints": [],
        })


def test_parse_rejects_anchor_with_neither_name_nor_term_type():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {}, "constraints": []})


def test_parse_rejects_missing_anchor():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"constraints": []})


def test_parse_name_anchor_allows_empty_constraints():
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}})
    assert args.constraints == []


def test_parse_type_anchor_rejects_empty_constraints_without_expand():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {"term_type": "SKU"}, "constraints": []})


def test_parse_type_anchor_rejects_empty_constraints_even_with_expand():
    """expand 不是过滤条件的替代品——TypeAnchor 模式下无约束全量扫描依然禁止，
    不因为设了 expand 就放行，见设计文档。"""
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({
            "anchor": {"term_type": "SKU"}, "constraints": [],
            "expand": {"hops": 1},
        })


def test_parse_expand_defaults():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {},
    })
    assert args.expand == ExpandSpec(hops=1, relation_type=None, direction="both")


def test_parse_expand_with_explicit_values():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {"hops": 2, "relation_type": "BELONG_TO", "direction": "outgoing"},
    })
    assert args.expand == ExpandSpec(hops=2, relation_type="BELONG_TO", direction="outgoing")


def test_parse_expand_rejects_invalid_hops():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {"name": "x"}, "expand": {"hops": 3}})


def test_parse_expand_rejects_bool_hops():
    """bool 是 int 的子类，True == 1、False == 0——不加 isinstance(..., bool) 排除的话，
    {"hops": True} 会静默通过 `hops not in _VALID_EXPAND_HOPS` 检查，之后被拼进 Cypher
    的 *1..True 模式段，产生非法 Cypher（被下游更宽的 except Exception 兜住，退化成一个
    不清楚的通用错误，而不是这里这条清晰的校验信息）。"""
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {"name": "x"}, "expand": {"hops": True}})


def test_parse_expand_rejects_invalid_direction():
    with pytest.raises(StructuredFilterQueryError):
        parse_structured_filter_query_args({"anchor": {"name": "x"}, "expand": {"direction": "sideways"}})


def test_parse_no_expand_defaults_to_none():
    args = parse_structured_filter_query_args({"anchor": {"name": "x"}})
    assert args.expand is None


def test_validate_name_anchor_does_not_require_term_type_schema_membership_for_type_hint():
    """type_hint 只是喂给 resolve_term 的消歧提示，不是需要预先确认的 schema 成员——
    resolved.term_type（解析后的真实类型）仍然要过 schema 校验，但 type_hint 本身不需要。"""
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola", "type_hint": "随便什么"}})
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="公司:Coca-Cola"),
        confirmed_relation_types=set(), term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )  # 不抛异常即通过——type_hint="随便什么" 不校验


def test_validate_rejects_resolved_term_type_not_in_schema():
    """防御性检查：resolve_term 解析出的 term_type 理论上应该在已确认 schema 里，
    但仍要检查，不能假定术语表和 schema 天然一致。"""
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}})
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="不存在的类型", node_key="x"),
            confirmed_relation_types=set(), term_type_schema={},
        )


def test_validate_expand_relation_type_must_be_confirmed():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {"relation_type": "NOT_CONFIRMED"},
    })
    with pytest.raises(StructuredFilterQueryError):
        validate_structured_filter_query(
            args, resolved=ResolvedAnchor(term_type="公司", node_key="x"),
            confirmed_relation_types={"BELONG_TO"},
            term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
        )


def test_validate_expand_relation_type_none_skips_confirmed_check():
    args = parse_structured_filter_query_args({"anchor": {"name": "coke-cola"}, "expand": {}})
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="x"),
        confirmed_relation_types=set(),
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )  # 不抛异常即通过——relation_type=None 不用查白名单


def test_validate_expand_relation_type_confirmed_passes():
    args = parse_structured_filter_query_args({
        "anchor": {"name": "coke-cola"},
        "expand": {"relation_type": "BELONG_TO"},
    })
    validate_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="x"),
        confirmed_relation_types={"BELONG_TO"},
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )  # 不抛异常即通过


from app.graphrag.structured_filter_query import (
    _maybe_resolve_attribute_constraint,
    _maybe_resolve_relation_constraint,
    _resolve_or_raise,
    _should_fuzzy_resolve,
)

_COMPANY_SCHEMA_STRING = TermTypeCategory(value="公司", extra_fields=[])
_SALES_SCHEMA_NUMBER_FOR_FUZZY_TEST = TermTypeCategory(
    value="销量", extra_fields=[], standard_name_value_type="number",
)


def test_should_fuzzy_resolve_true_for_standard_name_eq_string_type():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="eq", term_type="公司",
        term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    ) is True


def test_should_fuzzy_resolve_true_for_standard_name_ne():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="ne", term_type="公司",
        term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    ) is True


def test_should_fuzzy_resolve_false_for_non_standard_name_field():
    assert _should_fuzzy_resolve(
        field="numeric_value", operator="eq", term_type="SKU",
        term_type_schema={"SKU": _SKU_SCHEMA},
    ) is False


def test_should_fuzzy_resolve_false_for_starts_with():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="starts_with", term_type="公司",
        term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    ) is False


def test_should_fuzzy_resolve_false_when_standard_name_declared_number():
    """销量这类 value-as-node 类型的 standard_name 是数值类型，eq 比较的是真实
    数字，不能被误当成实体名解析——这是本次改动最容易踩的回归点。"""
    assert _should_fuzzy_resolve(
        field="standard_name", operator="eq", term_type="销量",
        term_type_schema={"销量": _SALES_SCHEMA_NUMBER_FOR_FUZZY_TEST},
    ) is False


def test_should_fuzzy_resolve_false_for_unknown_term_type():
    assert _should_fuzzy_resolve(
        field="standard_name", operator="eq", term_type="不存在的类型",
        term_type_schema={},
    ) is False


def test_resolve_or_raise_returns_standard_name_on_match():
    result = _resolve_or_raise("coke-cola", term_type="公司", terms=[_COKE_TERM])
    assert result == "Coca-Cola"


def test_resolve_or_raise_raises_when_not_found():
    with pytest.raises(StructuredFilterQueryError) as exc_info:
        _resolve_or_raise("完全不认识的名字", term_type="公司", terms=[_COKE_TERM])
    message = str(exc_info.value)
    assert "完全不认识的名字" in message
    assert "公司" in message


def test_resolve_or_raise_raises_when_value_not_a_string():
    with pytest.raises(StructuredFilterQueryError):
        _resolve_or_raise(123, term_type="公司", terms=[_COKE_TERM])


def test_resolve_or_raise_raises_when_match_is_wrong_term_type():
    """resolve_term 在 hint 类型下零命中时会退回全局匹配——_resolve_or_raise 必须
    拒绝跨类型命中的结果，不能把不同类型的术语当成解析成功。"""
    with pytest.raises(StructuredFilterQueryError):
        _resolve_or_raise("coke-cola", term_type="订单号", terms=[_COKE_TERM])


def test_maybe_resolve_attribute_constraint_replaces_value_when_applicable():
    constraint = AttributeConstraint(field="standard_name", operator="eq", value="coke-cola")
    result = _maybe_resolve_attribute_constraint(
        constraint, term_type="公司", terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    )
    assert result == AttributeConstraint(field="standard_name", operator="eq", value="Coca-Cola")


def test_maybe_resolve_attribute_constraint_passes_through_when_not_applicable():
    constraint = AttributeConstraint(field="numeric_value", operator="gt", value=500)
    result = _maybe_resolve_attribute_constraint(
        constraint, term_type="SKU", terms=[], term_type_schema={"SKU": _SKU_SCHEMA},
    )
    assert result is constraint  # 原样透传，不是重新构造的等价对象


def test_maybe_resolve_attribute_constraint_raises_on_unresolvable_value():
    constraint = AttributeConstraint(field="standard_name", operator="eq", value="完全不认识的名字")
    with pytest.raises(StructuredFilterQueryError):
        _maybe_resolve_attribute_constraint(
            constraint, term_type="公司", terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
        )


def test_maybe_resolve_relation_constraint_replaces_target_value_when_applicable():
    constraint = RelationConstraint(
        hops=[Hop(relation_type="BELONG_TO", direction="outgoing", target_term_type="公司")],
        target_field="standard_name", target_operator="eq", target_value="coke-cola",
    )
    result = _maybe_resolve_relation_constraint(
        constraint, terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    )
    assert result.target_value == "Coca-Cola"
    assert result.hops == constraint.hops  # 其余字段不变


def test_maybe_resolve_relation_constraint_passes_through_when_not_applicable():
    constraint = RelationConstraint(
        hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
        target_field="raw_value", target_operator="starts_with", target_value="红",
    )
    result = _maybe_resolve_relation_constraint(
        constraint, terms=[], term_type_schema={"VariantValue": _VARIANT_SCHEMA},
    )
    assert result is constraint


def test_maybe_resolve_relation_constraint_uses_last_hop_type_for_two_hop_chain():
    """两跳约束要用最后一跳的 target_term_type 做解析类型提示，不是第一跳的。"""
    constraint = RelationConstraint(
        hops=[
            Hop(relation_type="BELONG_TO", direction="outgoing", target_term_type="产品"),
            Hop(relation_type="BELONG_TO", direction="outgoing", target_term_type="公司"),
        ],
        target_field="standard_name", target_operator="eq", target_value="coke-cola",
    )
    result = _maybe_resolve_relation_constraint(
        constraint, terms=[_COKE_TERM], term_type_schema={"公司": _COMPANY_SCHEMA_STRING},
    )
    assert result.target_value == "Coca-Cola"


async def test_run_structured_filter_query_resolves_fuzzy_relation_constraint_value():
    """核心场景：anchor.term_type + constraints 里用口语化别名（"coke-cola"），
    第一次调用就应该能查出正确结果——不需要先用 anchor.name 消歧再发第二次调用。"""
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "0-7380-9438-2", "node_key": "订单号:0-7380-9438-2",
         "term_type": "订单号", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单号"},
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "BELONG_TO", "direction": "outgoing", "target_term_type": "公司"}],
                "target_field": "standard_name", "target_operator": "eq", "target_value": "coke-cola",
            }],
        },
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types={"BELONG_TO"},
        term_type_schema={
            "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
            "公司": TermTypeCategory(value="公司", extra_fields=[]),
        },
    )

    assert result["matched_count"] == 1
    # 断言真正传给图数据库执行层的约束值，已经是解析后的标准名"Coca-Cola"，
    # 不是 LLM 原始猜测的"coke-cola"——这是本次改动要验证的核心行为。
    resolved_constraint = graph_client.last_args.constraints[0]
    assert resolved_constraint.target_value == "Coca-Cola"


async def test_run_structured_filter_query_resolves_fuzzy_attribute_constraint_value():
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "公司"},
            "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": "coke-cola"}],
        },
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(),
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )

    assert result["matched_count"] == 1
    assert graph_client.last_args.constraints[0].value == "Coca-Cola"


async def test_run_structured_filter_query_returns_error_and_skips_execution_when_constraint_value_unresolvable():
    class _ExplodingGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            raise AssertionError("约束值解析失败时不应该查图谱")

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单号"},
            "constraints": [{
                "kind": "relation",
                "hops": [{"relation_type": "BELONG_TO", "direction": "outgoing", "target_term_type": "公司"}],
                "target_field": "standard_name", "target_operator": "eq", "target_value": "完全不认识的名字",
            }],
        },
        graph_client=_ExplodingGraphClient(), tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types={"BELONG_TO"},
        term_type_schema={
            "订单号": TermTypeCategory(value="订单号", extra_fields=[]),
            "公司": TermTypeCategory(value="公司", extra_fields=[]),
        },
    )

    assert "error" in result
    assert "完全不认识的名字" in result["error"]


async def test_run_structured_filter_query_numeric_standard_name_eq_unaffected_by_fuzzy_resolution():
    """销量这类 value-as-node 类型的 standard_name 是数值——eq 比较数字时，
    完全不应该触发模糊解析，行为要跟本次改动之前一模一样（防回归）。"""
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "100", "node_key": "销量:100", "term_type": "销量", "all_properties": {}},
    ])

    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "销量"},
            "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": 100}],
        },
        graph_client=graph_client, tenant_id="demo", terms=[],
        confirmed_relation_types=set(),
        term_type_schema={"销量": TermTypeCategory(
            value="销量", extra_fields=[], standard_name_value_type="number",
        )},
    )

    assert result["matched_count"] == 1
    # 数值 100 原样透传，不经过 _resolve_or_raise（terms=[] 也证明了这一点——
    # 如果误触发了模糊解析，空 terms 列表会导致解析失败报错，而不是正常返回结果）。
    assert graph_client.last_args.constraints[0].value == 100


async def test_run_structured_filter_query_name_anchor_constraints_also_resolve_fuzzy_values():
    """NameAnchor 模式下 constraints 里的模糊解析也要生效，不只是 TypeAnchor 模式。"""
    graph_client = _FakeGraphClient(rows=[
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ])

    await run_structured_filter_query(
        {
            "anchor": {"name": "coke-cola"},
            "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": "coke-cola"}],
        },
        graph_client=graph_client, tenant_id="demo", terms=[_COKE_TERM],
        confirmed_relation_types=set(),
        term_type_schema={"公司": TermTypeCategory(value="公司", extra_fields=[])},
    )

    assert graph_client.last_args.constraints[0].value == "Coca-Cola"
