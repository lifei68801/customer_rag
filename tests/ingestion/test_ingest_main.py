from app.config.settings import Settings
from app.ingestion.main import main
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.retrieval.vector_store import InMemoryVectorStore


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


def _settings() -> Settings:
    return Settings(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        milvus_uri="http://localhost:19530",
        milvus_collection="faq_chunks",
    )


async def test_main_ingests_directory_using_injected_registry_and_store(tmp_path):
    (tmp_path / "doc.md").write_text(
        "## 主题\n内容。\n", encoding="utf-8"
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("qwen-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    total = await main(
        directory=tmp_path,
        settings=_settings(),
        embedding_registry=embedding_registry,
        vector_store=vector_store,
    )

    assert total == 1
