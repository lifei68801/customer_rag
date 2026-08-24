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
    )
]


class FakeGraphClient:
    def __init__(self) -> None:
        self.queried_tenant_ids: list[str] = []
        self.queried_node_keys: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_tenant_ids.append(tenant_id)
        self.queried_node_keys.append(standard_name)
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


async def test_graph_query_tool_uses_entity_type_to_disambiguate():
    terms = [
        Term(tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee", aliases=[], term_type="产品"),
        Term(tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee", aliases=[], term_type="类目"),
    ]
    graph_client = FakeGraphClient()

    result = await graph_query_tool(
        "Coffee", terms=terms, tenant_id="t1", graph_client=graph_client, entity_type="类目",
    )

    assert result.resolved is True
    assert graph_client.queried_node_keys == ["类目:Coffee"]


async def test_graph_query_tool_returns_unresolved_when_ambiguous_without_entity_type():
    """两个同名不同类型的实体存在、又没有类型提示时，明确返回未命中，
    不能随便选一个回答给客户——这是本次改动要避免的"悄悄选错实体"风险，
    见该 bug 的调查记录。"""
    terms = [
        Term(tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee", aliases=[], term_type="产品"),
        Term(tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee", aliases=[], term_type="类目"),
    ]
    graph_client = FakeGraphClient()

    result = await graph_query_tool(
        "Coffee", terms=terms, tenant_id="t1", graph_client=graph_client,
    )

    assert result.resolved is False


async def test_graph_query_tool_resolves_alias_even_when_standard_name_collides_with_another_type():
    """entity_name 是某个类型下唯一匹配的别名，虽然它解析出的 standard_name
    恰好和另一个不同类型的术语撞名，但按"名字或别名"本身衡量并不存在歧义
    （全部术语里只有这一条的别名是这个词），应该正常解析成功，并且用的是
    这条别名真正对应的那个 Term 的 node_key，不能因为撞了 standard_name
    就随便选中另一个不相关的术语，也不能整个查询失败。

    如果实现照抄"先用 resolve_to_standard_name 拿到 standard_name 字符串，
    再用 find_term_by_type_hint 反查一次 node_key"这种两次独立查找的写法
    （两者的去重基准不同：前者按"候选名是否等于某术语的 standard_name 或
    在其 aliases 里"去重，后者只按"standard_name 字段在全部术语里是否
    唯一"去重），这里会因为 standard_name 撞名导致第二次查找判定"不唯一"
    而返回 None，进而让 `assert term is not None` 抛出未处理的异常——这正是
    app/graphrag/normalization.py `_resolve_term` 文档里记录的 2026-08-22
    Fix round 1 bug（normalize_and_write_relations 曾经就是这么写的，修复后
    改成只查一次，同一个 Term 对象同时提供 standard_name 和 node_key）。
    graph_query_tool 如果照抄同样的两次查找写法就会重新踩到同一个坑，因此
    改用 `resolve_term`（现在是 `app/graphrag/ontology.py` 的公开函数，
    见该函数文档）只查一次。
    """
    terms = [
        Term(
            tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee",
            aliases=["咖啡"], term_type="产品",
        ),
        Term(
            tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee",
            aliases=[], term_type="类目",
        ),
    ]
    graph_client = FakeGraphClient()

    result = await graph_query_tool(
        "咖啡", terms=terms, tenant_id="t1", graph_client=graph_client,
    )

    assert result.resolved is True
    assert result.standard_name == "Coffee"
    assert graph_client.queried_node_keys == ["产品:Coffee"]


def test_tool_schemas_do_not_expose_tenant_id():
    for schema in (VECTOR_SEARCH_TOOL_SCHEMA, GRAPH_QUERY_TOOL_SCHEMA):
        properties = schema["function"]["parameters"]["properties"]
        assert "tenant_id" not in properties


async def test_structured_filter_query_tool_delegates_to_run_structured_filter_query():
    from app.agent.tools import structured_filter_query_tool
    from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, tenant_id, term_type_schema):
            return {"rows": [], "total_count": 0}

    result = await structured_filter_query_tool(
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        tenant_id="muji", graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )},
    )

    assert result == {"matched_count": 0, "results": []}


def test_structured_filter_query_tool_schema_does_not_expose_tenant_id():
    from app.agent.tools import STRUCTURED_FILTER_QUERY_TOOL_SCHEMA
    properties = STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert "tenant_id" not in properties
