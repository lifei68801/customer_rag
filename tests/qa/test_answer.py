from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.qa.answer import answer_question
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

    vector_store = InMemoryVectorStore()
    await vector_store.upsert(
        [
            VectorRecord(
                id="faq/network.md",
                vector=[1.0, 0.0],
                text="网络断开时，请先重启路由器。",
                metadata={},
            ),
            VectorRecord(
                id="faq/login.md",
                vector=[0.0, 1.0],
                text="登录失败请检查账号密码。",
                metadata={},
            ),
        ]
    )

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        top_k=1,
    )

    assert result.text == "按资料所述，重启路由器即可解决。"
    assert result.used_sources == ["faq/network.md"]
    assert llm_provider.last_request is not None
    assert "重启路由器" in llm_provider.last_request.messages[0]["content"]
