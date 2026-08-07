from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankHit, RerankRequest, RerankResult
from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FixedEmbeddingProvider:
    """让每段文本映射到写死的向量，方便控制向量检索的排序结果。"""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=[self._mapping.get(t, [0.0, 0.0]) for t in request.texts]
        )


async def _build_store_and_index() -> tuple[InMemoryVectorStore, BM25Index]:
    records = [
        VectorRecord(
            id="vector_hit",
            vector=[1.0, 0.0],
            text="这是语义上很接近的内容，但不含关键词。",
            tenant_id="t1",
            metadata={},
        ),
        VectorRecord(
            id="bm25_hit",
            vector=[0.0, 0.0],  # 向量检索故意让它排不进去
            text="错误码 E502 表示网关超时",
            tenant_id="t1",
            metadata={},
        ),
    ]
    store = InMemoryVectorStore()
    await store.upsert(records)
    bm25 = BM25Index()
    bm25.index(records)
    return store, bm25


async def test_hybrid_search_surfaces_bm25_only_hit_via_fusion():
    store, bm25 = await _build_store_and_index()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        "fake-embedding",
        FixedEmbeddingProvider({"E502 错误码是什么意思": [1.0, 0.0]}),
    )

    llm_registry = ProviderRegistry()  # 不注册任何 provider，改写应直接跳过

    results = await hybrid_search(
        "E502 错误码是什么意思",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=store,
        bm25_index=bm25,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        query_rewrite_enabled=False,
        final_top_k=2,
        tenant_id="t1",
    )

    result_ids = {record.id for record in results}
    assert "bm25_hit" in result_ids
    assert "vector_hit" in result_ids


async def test_hybrid_search_does_not_leak_another_tenants_records():
    store, bm25 = await _build_store_and_index()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        "fake-embedding",
        FixedEmbeddingProvider({"E502 错误码是什么意思": [1.0, 0.0]}),
    )
    llm_registry = ProviderRegistry()

    results = await hybrid_search(
        "E502 错误码是什么意思",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=store,
        bm25_index=bm25,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        query_rewrite_enabled=False,
        final_top_k=2,
        tenant_id="t2",
    )

    assert results == []


async def test_hybrid_search_passes_conversation_context_to_query_rewrite():
    store, bm25 = await _build_store_and_index()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        "fake-embedding",
        FixedEmbeddingProvider({"E502 错误码是什么意思": [1.0, 0.0]}),
    )

    class RecordingLLMProvider:
        def __init__(self) -> None:
            self.last_request: ProviderRequest | None = None

        async def complete(self, request: ProviderRequest) -> ProviderResult:
            self.last_request = request
            return ProviderResult(text="E502 错误码是什么意思")

    llm_provider = RecordingLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "llm", llm_provider)

    conversation_context = [{"role": "user", "content": "我遇到了E502"}]

    await hybrid_search(
        "这个报错是什么意思",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=store,
        bm25_index=bm25,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        query_rewrite_enabled=True,
        final_top_k=2,
        tenant_id="t1",
        conversation_context=conversation_context,
    )

    assert llm_provider.last_request is not None
    assert conversation_context[0] in llm_provider.last_request.messages


async def test_hybrid_search_uses_rerank_order_when_provided():
    store, bm25 = await _build_store_and_index()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        "fake-embedding",
        FixedEmbeddingProvider({"E502 错误码是什么意思": [1.0, 0.0]}),
    )
    llm_registry = ProviderRegistry()

    class FakeRerankProvider:
        async def rerank(self, request: RerankRequest) -> RerankResult:
            # 强制把 bm25_hit 排到最后，验证最终顺序确实来自 rerank 而非融合顺序
            hits = [
                RerankHit(index=i, relevance_score=1.0 - i * 0.1)
                for i, doc in enumerate(request.documents)
                if "网关超时" not in doc
            ]
            hits += [
                RerankHit(index=i, relevance_score=0.01)
                for i, doc in enumerate(request.documents)
                if "网关超时" in doc
            ]
            return RerankResult(hits=hits)

    results = await hybrid_search(
        "E502 错误码是什么意思",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=store,
        bm25_index=bm25,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        query_rewrite_enabled=False,
        rerank_provider=FakeRerankProvider(),
        final_top_k=2,
        tenant_id="t1",
    )

    assert results[-1].id == "bm25_hit"


async def test_hybrid_search_rewrites_score_with_rerank_relevance():
    store, bm25 = await _build_store_and_index()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        "fake-embedding",
        FixedEmbeddingProvider({"E502 错误码是什么意思": [1.0, 0.0]}),
    )
    llm_registry = ProviderRegistry()

    class FakeRerankProvider:
        async def rerank(self, request: RerankRequest) -> RerankResult:
            # relevance_score 和融合排序位置无关，专门设成能一眼看出是不是被
            # 正确回写的值（0.42/0.17），验证下游拿到的是这两个数而不是向量
            # 检索阶段的原始相似度。
            hits = [
                RerankHit(index=i, relevance_score=0.42 if "网关超时" not in doc else 0.17)
                for i, doc in enumerate(request.documents)
            ]
            return RerankResult(hits=hits)

    results = await hybrid_search(
        "E502 错误码是什么意思",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=store,
        bm25_index=bm25,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        query_rewrite_enabled=False,
        rerank_provider=FakeRerankProvider(),
        final_top_k=2,
        tenant_id="t1",
    )

    scores_by_id = {record.id: record.score for record in results}
    assert scores_by_id["vector_hit"] == 0.42
    assert scores_by_id["bm25_hit"] == 0.17
