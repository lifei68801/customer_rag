from app.graphrag.ontology import Term
from app.graphrag.term_guard import build_term_guard_context

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
        product_line="核心平台",
    ),
]


class FakeGraphClient:
    def __init__(self, subgraph_rows: list[dict]) -> None:
        self._rows = subgraph_rows
        self.queried_names: list[str] = []
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_names.append(standard_name)
        self.queried_tenant_ids.append(tenant_id)
        return self._rows


async def test_returns_none_when_no_term_matched():
    graph_client = FakeGraphClient(subgraph_rows=[])

    context = await build_term_guard_context(
        "今天天气怎么样", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert context is None
    assert graph_client.queried_names == []


async def test_returns_context_and_queries_graph_when_term_matched():
    graph_client = FakeGraphClient(
        subgraph_rows=[{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    )

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert context is not None
    assert "错误码E502" in context
    assert "登录模块" in context
    assert graph_client.queried_names == ["错误码E502"]
    assert graph_client.queried_tenant_ids == ["t1"]
