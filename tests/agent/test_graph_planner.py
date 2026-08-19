from app.agent.graph import build_agent_graph
from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult, ToolCall
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class ScriptedLLMProvider:
    def __init__(self, responses: list[ProviderResult]) -> None:
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        return self._responses.pop(0)


def _dependencies(tenant_id: str = "t1"):
    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时请先重启路由器。",
            tenant_id=tenant_id,
            metadata={},
        )
    ]
    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    return records, vector_store, bm25_index, embedding_registry


async def _seed(records, vector_store, bm25_index) -> None:
    await vector_store.upsert(records)
    bm25_index.index(records)


async def test_planner_calls_tool_once_then_answers():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="vector_search_tool",
                            arguments='{"query": "网络连不上怎么办"}',
                        )
                    ],
                ),
                ProviderResult(text="重启路由器即可解决。"),
            ]
        ),
    )

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "tenant_id": "t1"}
    )

    assert result["final_text"] == "重启路由器即可解决。"
    assert result["used_sources"] == ["faq/network.md"]
    assert result["fallback_triggered"] is False
    assert result.get("ticket_id") is None


async def test_planner_exceeding_max_rounds_falls_back_and_creates_ticket():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

    # 每一轮都要求调用工具，永远不给出最终答案——用来验证轮次上限生效。
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(id=f"call_{i}", name="vector_search_tool", arguments='{"query": "x"}')
                    ],
                )
                for i in range(1, 3)
            ]
        ),
    )

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
        max_tool_call_rounds=1,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "tenant_id": "t1"},
        config={"recursion_limit": 50},
    )

    assert result["fallback_triggered"] is True
    assert result["ticket_id"]
    assert "转" in result["final_text"] or "人工" in result["final_text"]


async def test_planner_does_not_surface_another_tenants_records():
    records, vector_store, bm25_index, embedding_registry = _dependencies(tenant_id="t1")
    await _seed(records, vector_store, bm25_index)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="vector_search_tool",
                            arguments='{"query": "网络连不上怎么办", "tenant_id": "t1"}',
                        )
                    ],
                ),
                ProviderResult(text="没有找到相关资料。"),
            ]
        ),
    )

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "tenant_id": "t2"}
    )

    assert result["used_sources"] == []


async def test_planner_graph_uses_graph_query_tool_with_term_guard_context():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="graph_query_tool",
                            arguments='{"entity_name": "网关超时示例"}',
                        )
                    ],
                ),
                ProviderResult(text="已确认标准名称是示例错误码E502。"),
            ]
        ),
    )
    terms = [
        Term(
            tenant_id="t1",
            node_key="示例错误码E502",
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
        )
    ]

    class FakeGraphClient:
        async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
            return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
        terms=terms,
        graph_client=FakeGraphClient(),
    )

    result = await graph.ainvoke(
        {"question": "网关超时示例是什么", "tenant_id": "t1"}
    )

    assert result["final_text"] == "已确认标准名称是示例错误码E502。"
