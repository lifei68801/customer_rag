from app.agent.tools import (
    STRUCTURED_FILTER_QUERY_TOOL_SCHEMA,
    VECTOR_SEARCH_TOOL_SCHEMA,
    vector_search_tool,
)
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


def test_tool_schemas_do_not_expose_tenant_id():
    for schema in (VECTOR_SEARCH_TOOL_SCHEMA, STRUCTURED_FILTER_QUERY_TOOL_SCHEMA):
        properties = schema["function"]["parameters"]["properties"]
        assert "tenant_id" not in properties


async def test_structured_filter_query_tool_delegates_to_run_structured_filter_query():
    from app.agent.tools import structured_filter_query_tool
    from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            return {"rows": [], "total_count": 0}

    result = await structured_filter_query_tool(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        tenant_id="muji", terms=[], graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )},
    )

    # 注意：这里不带 "truncated" 键——truncated 只在 total_count > len(rows) 时才会
    # 出现在 payload 里（见 structured_filter_query.py::run_structured_filter_query
    # 尾部），total_count == len(rows) == 0 时该键不存在。brief 给的字面测试代码里
    # 这里写的是 {"matched_count": 0, "truncated": False, "anchors": []}，那个形状
    # 实际上只出现在 NameAnchor 未解析时的提前返回分支（同文件 364 行），不适用于
    # 这里的 TypeAnchor 直通场景——已按 Task 7 已合入、已 review 的真实行为改正。
    assert result == {"matched_count": 0, "anchors": []}


async def test_structured_filter_query_tool_resolves_name_anchor():
    from app.agent.tools import structured_filter_query_tool
    from app.graphrag.ontology import Term
    from app.graphrag.ontology_categories import TermTypeCategory

    terms = [Term(
        tenant_id="t1", node_key="示例错误码E502", standard_name="示例错误码E502",
        aliases=["网关超时示例"], term_type="error_code",
    )]

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            assert resolved.node_key == "示例错误码E502"
            return {"rows": [{
                "standard_name": "示例错误码E502", "node_key": "示例错误码E502",
                "term_type": "error_code", "all_properties": {},
            }], "total_count": 1}

    result = await structured_filter_query_tool(
        {"anchor": {"name": "网关超时示例"}},
        tenant_id="t1", terms=terms, graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    assert result["matched_count"] == 1
    assert result["anchors"][0]["standard_name"] == "示例错误码E502"


def test_structured_filter_query_tool_schema_only_exposes_query_intent():
    from app.agent.tools import STRUCTURED_FILTER_QUERY_TOOL_SCHEMA

    properties = STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert set(properties) == {"query_intent"}
    assert STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["parameters"]["required"] == ["query_intent"]
    # 详细能力说明（anchor/constraints/hops 这套结构化机制）不应该出现在
    # 对外暴露的 description 里——这是渐进式披露的核心：第一次推理调用
    # 只看到"用自然语言描述想查什么"，不需要理解结构化字段本身。
    description = STRUCTURED_FILTER_QUERY_TOOL_SCHEMA["function"]["description"]
    for forbidden in ("anchor", "constraints", "hops", "matched_count"):
        assert forbidden not in description
    assert "graph_query_tool" not in str(STRUCTURED_FILTER_QUERY_TOOL_SCHEMA)


def test_structured_filter_query_usage_guide_and_full_schema_preserve_detail():
    from app.agent.tools import (
        STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA,
        STRUCTURED_FILTER_QUERY_USAGE_GUIDE,
    )

    # 详细机制说明搬到这两个常量里，供独立参数生成调用引用——内容本身
    # 还在，只是不再暴露在对外的工具 schema 里。
    assert "anchor" in STRUCTURED_FILTER_QUERY_USAGE_GUIDE
    assert "constraints" in STRUCTURED_FILTER_QUERY_USAGE_GUIDE
    properties = STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA["properties"]
    assert "anchor" in properties
    assert "constraints" in properties
    assert "expand" in properties
