import yaml

from app.agent.tool_registry import ToolContext
from app.agent.tools.vector_search.tool import TOOL
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


def _manifest_path():
    import app.agent.tools.vector_search as pkg
    from pathlib import Path
    return Path(pkg.__file__).parent / "manifest.yaml"


def test_manifest_schema_does_not_expose_tenant_id():
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert "tenant_id" not in raw["parameters_schema"]["properties"]


async def test_execute_returns_records_scoped_to_tenant():
    records = [
        VectorRecord(
            id="faq/network.md", vector=[1.0, 0.0], text="网络断开时请先重启路由器。",
            tenant_id="t1", metadata={},
        ),
        VectorRecord(
            id="faq/other-tenant.md", vector=[1.0, 0.0], text="属于别的租户的资料",
            tenant_id="t2", metadata={},
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

    context = ToolContext(
        tenant_id="t1", question="网络连不上怎么办？",
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
        vector_store=vector_store, bm25_index=bm25_index,
        llm_registry=llm_registry, llm_provider_name="fake-llm",
        rerank_provider=None, query_rewrite_enabled=False,
        terms=[], graph_client=None, confirmed_relation_types=set(),
        term_type_schema={}, allowed_combinations=[],
    )

    resolved = await TOOL.resolve_arguments({"query": "网络连不上怎么办？"}, context=context)
    observation, records = await TOOL.execute(resolved, context=context)

    assert [r["id"] for r in observation["results"]] == ["faq/network.md"]
    assert [r.id for r in records] == ["faq/network.md"]
