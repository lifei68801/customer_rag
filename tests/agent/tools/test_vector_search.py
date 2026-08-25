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


def test_manifest_description_matches_content_exactly():
    """精确逐字符比对（不是子串检查）——manifest.yaml 里如果用 YAML 折叠
    标量（`>`）跨行写多行中文，换行处会被折叠成一个空格、结尾还会带一个
    多余的换行符，这跟原始 Python 字符串字面量拼接完全不是一回事（中文
    本来就不需要词间空格）。这条测试直接钉死解析结果，任何回归（比如
    改回折叠标量）都会在这里失败，不依赖人工重新逐字核对。"""
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert raw["description"] == (
        "在企业知识库中做混合检索（向量+关键词），返回相关文档片段。"
        "当需要补充事实性资料来回答用户问题时调用。"
    )


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
