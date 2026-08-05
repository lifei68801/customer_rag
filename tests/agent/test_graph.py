from app.agent.graph import build_agent_graph
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class FakeLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_request: ProviderRequest | None = None

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.last_request = request
        return ProviderResult(text=self._text)


async def _build_dependencies(*, with_records: bool, llm_text: str):
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()
    if with_records:
        records = [
            VectorRecord(
                id="faq/network.md",
                vector=[1.0, 0.0],
                text="网络断开时，请先重启路由器。",
                metadata={},
            )
        ]
        await vector_store.upsert(records)
        bm25_index.index(records)

    llm_provider = FakeLLMProvider(llm_text)
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    return embedding_registry, vector_store, bm25_index, llm_registry, llm_provider


async def test_happy_path_returns_llm_answer_with_used_sources():
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(with_records=True, llm_text="重启路由器即可解决。")
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    result = await graph.ainvoke({"question": "网络连不上怎么办？"})

    assert result["final_text"] == "重启路由器即可解决。"
    assert result["used_sources"] == ["faq/network.md"]
    assert result.get("ticket_id") is None


async def test_no_records_triggers_fallback_and_creates_ticket():
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(with_records=False, llm_text="不应该被用到")
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    result = await graph.ainvoke({"question": "完全无关的问题"})

    assert result["fallback_triggered"] is True
    assert result["ticket_id"]
    assert "转" in result["final_text"] or "人工" in result["final_text"]
    assert llm_provider.last_request is None


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


async def test_semantic_review_flags_output_as_unsafe():
    embedding_registry, vector_store, bm25_index, llm_registry, _ = (
        await _build_dependencies(with_records=True, llm_text="占位")
    )
    scripted = ScriptedLLMProvider(
        ["这是回答内容。", '{"is_safe": false, "reason": "测试触发"}']
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", scripted)

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    result = await graph.ainvoke({"question": "网络连不上怎么办？"})

    assert result["final_text"] == "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"
    assert result["semantic_review_reviewed"] is True


async def test_unsafe_input_short_circuits_without_calling_llm():
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(with_records=True, llm_text="不应该被用到")
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        banned_terms=["敏感词"],
    )

    result = await graph.ainvoke({"question": "这里面有敏感词"})

    assert result["is_input_safe"] is False
    assert llm_provider.last_request is None
    assert result["final_text"] != "不应该被用到"
