from app.graphrag.neptune_client import NeptuneGraphClient
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.structured_filter_query import AttributeConstraint, ResolvedAnchor, TypeAnchor


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


async def test_ensure_tenant_scoped_schema_runs_backfill():
    client_stub = FakeNeptuneClient(rows=[])
    client = NeptuneGraphClient(client=client_stub)

    await client.ensure_tenant_scoped_schema()

    assert any("tenant_id" in q and "IS NULL" in q for q, _ in client_stub.calls)
