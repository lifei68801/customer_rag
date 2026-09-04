import pytest

from app.graphrag.neptune_client import NeptuneGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.graphrag.structured_filter_query import AttributeConstraint, ExpandSpec, ResolvedAnchor, TypeAnchor


class FakeNeptuneClient:
    """execute_open_cypher 直接返回行列表（已经是 openCypher HTTPS 端点
    JSON 响应体里 "results" 字段解析完的形状），不模拟 session/transaction——
    Neptune 的 openCypher HTTPS API 本身是单次请求-响应，没有这个概念。"""

    def __init__(self, rows: list[dict] | None = None, *, call_results: list | None = None) -> None:
        self._rows = rows if rows is not None else []
        self._call_results = call_results
        self._call_index = 0
        self.last_query: str | None = None
        self.last_parameters: dict | None = None
        self.calls: list[tuple[str, dict]] = []

    async def execute_open_cypher(self, query: str, parameters: dict | None = None) -> list[dict]:
        self.last_query = query
        self.last_parameters = parameters
        self.calls.append((query, parameters))
        if self._call_results is not None:
            result = self._call_results[self._call_index]
            self._call_index += 1
            return result if isinstance(result, list) else [result]
        return self._rows


async def test_query_subgraph_returns_related_terms():
    client_stub = FakeNeptuneClient(
        rows=[{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    )
    client = NeptuneGraphClient(client=client_stub)

    results = await client.query_subgraph("错误码E502", tenant_id="t1")

    assert results == [{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    assert client_stub.last_parameters == {"node_key": "错误码E502", "tenant_id": "t1"}
    assert "WHERE r.tenant_id = $tenant_id" in client_stub.last_query


async def test_execute_structured_filter_query_simple_attribute_constraint():
    client_stub = FakeNeptuneClient(
        call_results=[
            [{"total": 1}],
            [{"standard_name": "红茶拿铁", "node_key": "产品:红茶拿铁",
              "term_type": "产品", "all_properties": {"price": 18}}],
        ]
    )
    client = NeptuneGraphClient(client=client_stub)
    args = AttributeConstraint(field="price", operator="lt", value=20)

    class _Args:
        constraints = [args]
        group_by = None
        expand = None
        anchor = TypeAnchor(term_type="产品")
        limit = 5

    result = await client.execute_structured_filter_query(
        _Args(),
        resolved=ResolvedAnchor(node_key=None, term_type="产品"),
        tenant_id="t1",
        term_type_schema={},
    )

    assert result["total_count"] == 1
    assert result["rows"][0]["standard_name"] == "红茶拿铁"
    assert len(client_stub.calls) == 2  # 一次 count，一次取行


async def test_execute_structured_filter_query_anchor_by_node_key():
    client_stub = FakeNeptuneClient(call_results=[[{"total": 0}], []])
    client = NeptuneGraphClient(client=client_stub)

    class _Args:
        constraints = []
        group_by = None
        expand = None
        anchor = None
        limit = 5

    await client.execute_structured_filter_query(
        _Args(),
        resolved=ResolvedAnchor(node_key="产品:红茶拿铁", term_type="产品"),
        tenant_id="t1",
        term_type_schema={},
    )

    count_query, count_params = client_stub.calls[0]
    assert "anchor_node_key" in count_params
    assert count_params["anchor_node_key"] == "产品:红茶拿铁"


async def test_execute_structured_filter_query_builds_relation_exists_subquery():
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    client_stub = FakeNeptuneClient(call_results=[[{"total": 0}], []])
    client = NeptuneGraphClient(client=client_stub)
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
            target_field="raw_value", target_operator="eq", target_value="红",
        )],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
        },
    )

    count_query, count_params = client_stub.calls[0]
    assert "EXISTS {" in count_query
    assert "-[:HAS_VARIANT]->" in count_query
    assert "c0_hop0.raw_value = $c0_target_value" in count_query
    assert "c0_target_field" not in count_params
    assert count_params["c0_target_value"] == "红"


async def test_execute_structured_filter_query_relation_constraint_incoming_direction_reverses_arrow():
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    client_stub = FakeNeptuneClient(call_results=[[{"total": 0}], []])
    client = NeptuneGraphClient(client=client_stub)
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="VariantValue"),
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="incoming", target_term_type="SKU")],
            target_field="price", target_operator="gt", target_value=0,
        )],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="VariantValue", node_key=None), tenant_id="muji",
        term_type_schema={
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
        },
    )

    count_query, _ = client_stub.calls[0]
    assert "<-[:HAS_VARIANT]-" in count_query


async def test_ensure_tenant_scoped_schema_runs_backfill():
    client_stub = FakeNeptuneClient(rows=[])
    client = NeptuneGraphClient(client=client_stub)

    await client.ensure_tenant_scoped_schema()

    assert any("tenant_id" in q and "IS NULL" in q for q, _ in client_stub.calls)


async def test_execute_structured_filter_query_group_by_returns_aggregated_groups():
    from app.graphrag.structured_filter_query import GroupBy, Hop, RelationConstraint, StructuredFilterQueryArgs

    client_stub = FakeNeptuneClient(rows=[{"value": "红色", "count": 12}, {"value": "白色", "count": 8}])
    client = NeptuneGraphClient(client=client_stub)
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
            target_field="raw_value", target_operator="eq", target_value="__group__",
        )],
        expand=None, group_by=GroupBy(constraint_index=0), limit=20,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
        },
    )

    # group_by 分支只发一次 execute_open_cypher（没有单独的 count 请求——聚合
    # 结果本身就是"总数"，不需要像非 group_by 分支那样先查 total 再查 rows）。
    assert len(client_stub.calls) == 1
    assert result == {"groups": [{"value": "红色", "count": 12}, {"value": "白色", "count": 8}]}
    assert "count(DISTINCT anchor)" in client_stub.last_query
    assert "RETURN g0_hop0.raw_value AS value" in client_stub.last_query
    assert "group_field" not in client_stub.last_parameters


async def test_execute_structured_filter_query_expand_includes_optional_match_and_limit_param():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    client_stub = FakeNeptuneClient(call_results=[
        [{"total": 1}],
        [{"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []}],
    ])
    client = NeptuneGraphClient(client=client_stub)
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type="BELONG_TO", direction="outgoing"),
        group_by=None, limit=5,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    rows_query, rows_params = client_stub.calls[-1]
    assert "OPTIONAL MATCH" in rows_query
    assert "-[r:BELONG_TO*1..1]->" in rows_query
    assert "collect(DISTINCT CASE WHEN neighbor IS NULL THEN NULL" in rows_query
    # LIMIT 必须约束的是锚点数、不是展开后的行数——WITH...LIMIT 必须出现在
    # OPTIONAL MATCH 之前。
    assert rows_query.index("LIMIT $limit") < rows_query.index("OPTIONAL MATCH")
    assert rows_params["limit"] == 5
    assert result["rows"][0]["neighbors"] == []


# 后台管理写接口（GraphWriteProtocol，见 neo4j_client.py）在 NeptuneGraphClient
# 上尚未实现——这 7 个测试确认每个方法都报清晰的 NotImplementedError，而不是
# 调用方撞上一个没有说明的 AttributeError。见 2026-08-27 架构评审："收窄协议
# 本身不会在 CI 里拦住这类调用（本项目 CI 只跑 pytest，不跑类型检查），存根
# 才是运行时真正生效的防线"。


async def test_sync_term_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())
    term = Term(
        tenant_id="t1", node_key="k1", standard_name="错误码E502",
        aliases=[], term_type="error_code",
    )

    with pytest.raises(NotImplementedError, match="sync_term"):
        await client.sync_term(term)


async def test_rename_term_node_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())

    with pytest.raises(NotImplementedError, match="rename_term_node"):
        await client.rename_term_node(tenant_id="t1", node_key="k1", new_standard_name="新名字")


async def test_delete_term_node_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())

    with pytest.raises(NotImplementedError, match="delete_term_node"):
        await client.delete_term_node(tenant_id="t1", node_key="k1")


async def test_count_relation_edges_for_term_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())

    with pytest.raises(NotImplementedError, match="count_relation_edges_for_term"):
        await client.count_relation_edges_for_term(tenant_id="t1", node_key="k1")


async def test_ensure_extra_field_indexes_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())

    with pytest.raises(NotImplementedError, match="ensure_extra_field_indexes"):
        await client.ensure_extra_field_indexes(
            tenant_id="t1", term_type="产品",
            extra_fields=[ExtraFieldSpec(name="price", value_type="number")],
        )


async def test_migrate_relation_type_edges_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())

    with pytest.raises(NotImplementedError, match="migrate_relation_type_edges"):
        await client.migrate_relation_type_edges(tenant_id="t1", old_type="OLD", new_type="NEW")


async def test_migrate_term_type_nodes_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())

    with pytest.raises(NotImplementedError, match="migrate_term_type_nodes"):
        await client.migrate_term_type_nodes(tenant_id="t1", old_type="旧类型", new_type="新类型")


async def test_probe_relation_fanout_returns_max_distinct_targets():
    client_stub = FakeNeptuneClient(rows=[{"fanout": 3}])
    client = NeptuneGraphClient(client=client_stub)

    fanout = await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    )

    assert fanout == 3
    assert client_stub.last_parameters == {
        "tenant_id": "demo", "from_term_type": "产品", "to_term_type": "公司",
    }
    assert "(a:Term)-[r:BELONG_TO]->(b:Term)" in client_stub.last_query
    assert "$from_term_type" in client_stub.last_query
    assert "r.tenant_id = $tenant_id" in client_stub.last_query


async def test_probe_relation_fanout_flips_the_pattern_for_incoming():
    client_stub = FakeNeptuneClient(rows=[{"fanout": 1}])
    client = NeptuneGraphClient(client=client_stub)

    await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="公司", to_term_type="产品", direction="incoming",
    )

    assert "(a:Term)<-[r:BELONG_TO]-(b:Term)" in client_stub.last_query


async def test_probe_relation_fanout_returns_zero_when_no_edges_match():
    client_stub = FakeNeptuneClient(rows=[{"fanout": None}])
    client = NeptuneGraphClient(client=client_stub)

    assert await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    ) == 0


async def test_delete_relation_edge_raises_not_implemented():
    client = NeptuneGraphClient(client=FakeNeptuneClient())

    with pytest.raises(NotImplementedError, match="delete_relation_edge"):
        await client.delete_relation_edge(
            tenant_id="t1", subject_node_key="a", relation_type="RELATED_TO",
            object_node_key="b",
        )
