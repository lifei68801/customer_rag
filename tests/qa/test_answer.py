from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.qa.answer import answer_question
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class FakeLLMProvider:
    def __init__(self) -> None:
        self.last_request: ProviderRequest | None = None

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.last_request = request
        return ProviderResult(text="按资料所述，重启路由器即可解决。")


async def test_answer_question_uses_retrieved_context_in_the_prompt():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
        VectorRecord(
            id="faq/login.md",
            vector=[0.0, 1.0],
            text="登录失败请检查账号密码。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        top_k=1,
        tenant_id="t1",
    )

    assert result.text == "按资料所述，重启路由器即可解决。"
    assert result.used_sources == ["faq/network.md"]
    assert result.retrieved_context == "网络断开时，请先重启路由器。"
    assert llm_provider.last_request is not None
    assert "重启路由器" in llm_provider.last_request.messages[0]["content"]


class FakeGraphClient:
    async def query_subgraph(self, standard_name: str) -> list[dict]:
        return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]


async def test_answer_question_injects_term_guard_context_when_term_matched():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    terms = [
        Term(
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
            product_line="示例产品线",
        )
    ]

    await answer_question(
        "我这边报了网关超时示例，麻烦看下",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        terms=terms,
        graph_client=FakeGraphClient(),
        top_k=1,
        tenant_id="t1",
    )

    assert llm_provider.last_request is not None
    prompt = llm_provider.last_request.messages[0]["content"]
    assert "示例错误码E502" in prompt
    assert "示例登录模块" in prompt
