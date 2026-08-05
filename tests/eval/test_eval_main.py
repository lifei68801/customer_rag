from app.config.settings import Settings
from app.eval.runner import main
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


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


async def test_main_runs_eval_suite_using_injected_registry_and_store(tmp_path):
    dataset_path = tmp_path / "seed.jsonl"
    dataset_path.write_text(
        '{"question": "网络连不上怎么办？", "expected_answer": "重启路由器", '
        '"expected_sources": ["faq/network.md"]}\n',
        encoding="utf-8",
    )

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时请先重启路由器。",
            tenant_id="t1",
            metadata={},
        )
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("qwen-embedding", FakeEmbeddingProvider())

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "qwen",
        ScriptedLLMProvider(
            [
                "重启路由器即可解决。",
                '{"score": 0.9}',
                '{"score": 0.8}',
            ]
        ),
    )

    report = await main(
        dataset_path=dataset_path,
        tenant_id="t1",
        settings=_settings(),
        embedding_registry=embedding_registry,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
    )

    assert len(report.case_results) == 1
    assert report.average_context_recall == 1.0
