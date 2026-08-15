from app.agent.tools import (
    GRAPH_QUERY_TOOL_SCHEMA,
    VECTOR_SEARCH_TOOL_SCHEMA,
    graph_query_tool,
    vector_search_tool,
)
from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class FakeLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text="不应被用于查询改写")


async def test_vector_search_tool_returns_records_scoped_to_tenant():
    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
        VectorRecord(
            id="faq/other-tenant.md",
            vector=[1.0, 0.0],
            text="属于别的租户的资料",
            tenant_id="t2",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", FakeLLMProvider())

    results = await vector_search_tool(
        "网络连不上怎么办？",
        tenant_id="t1",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    assert [r.id for r in results] == ["faq/network.md"]


_TERMS = [
    Term(
        tenant_id="t1", node_key="示例错误码E502",
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
        product_line="示例产品线",
    )
]


class FakeGraphClient:
    def __init__(self) -> None:
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_tenant_ids.append(tenant_id)
        return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]


async def test_graph_query_tool_resolves_alias_and_returns_subgraph():
    graph_client = FakeGraphClient()
    result = await graph_query_tool(
        "网关超时示例", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert result.resolved is True
    assert result.standard_name == "示例错误码E502"
    assert result.subgraph == [
        {"related_name": "示例登录模块", "relation_type": "RELATED_TO"}
    ]
    assert graph_client.queried_tenant_ids == ["t1"]


async def test_graph_query_tool_returns_unresolved_without_querying_graph():
    class ExplodingGraphClient:
        async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
            raise AssertionError("未命中术语表时不应该查图谱")

    result = await graph_query_tool(
        "完全不认识的名字", terms=_TERMS, tenant_id="t1", graph_client=ExplodingGraphClient()
    )

    assert result.resolved is False
    assert result.standard_name is None
    assert result.subgraph == []


def test_tool_schemas_do_not_expose_tenant_id():
    for schema in (VECTOR_SEARCH_TOOL_SCHEMA, GRAPH_QUERY_TOOL_SCHEMA):
        properties = schema["function"]["parameters"]["properties"]
        assert "tenant_id" not in properties
