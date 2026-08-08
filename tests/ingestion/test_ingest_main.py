import aiosqlite

from app.config.settings import Settings
from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema, list_pending_reviews
from app.ingestion.main import main
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
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
        tenant_id="t1",
        settings=_settings(),
        embedding_registry=embedding_registry,
        vector_store=vector_store,
    )

    assert total == 1


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.synced_terms: list[Term] = []
        self.deleted_sources: list[tuple[str, str]] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append((source, tenant_id))

    async def sync_terms(self, terms: list[Term]) -> None:
        self.synced_terms.extend(terms)


async def test_main_sends_unresolved_graph_candidates_to_injected_review_conn(
    tmp_path,
):
    (tmp_path / "doc.md").write_text(
        "## 主题\n网关超时示例通常与不存在的实体相关。\n", encoding="utf-8"
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("qwen-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    graph_llm_registry = ProviderRegistry()
    graph_llm_registry.register(
        ProviderCapability.LLM,
        "qwen",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "不存在的实体", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_terms = [
        Term(
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
            product_line="示例产品线",
        )
    ]
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)

    await main(
        directory=tmp_path,
        tenant_id="t1",
        build_graph=True,
        settings=_settings(),
        embedding_registry=embedding_registry,
        vector_store=vector_store,
        graph_llm_registry=graph_llm_registry,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=review_conn,
    )

    assert graph_client.written == []
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["object_candidate"] == "不存在的实体"


async def test_main_syncs_ontology_terms_into_graph_when_build_graph(tmp_path):
    """--build-graph 时应该先把术语表（标准节点+别名）同步进图谱，而不是
    只写 LLM 抽取出的关系——否则图谱里除了关系边，标准节点/别名信息全靠
    YAML 文件，Neo4j 里查不到。"""
    (tmp_path / "doc.md").write_text("## 主题\n无关内容。\n", encoding="utf-8")

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("qwen-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    graph_llm_registry = ProviderRegistry()
    graph_llm_registry.register(
        ProviderCapability.LLM, "qwen", FixedLLMProvider('{"relations": []}')
    )
    graph_terms = [
        Term(
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
            product_line="示例产品线",
        )
    ]
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)

    await main(
        directory=tmp_path,
        tenant_id="t1",
        build_graph=True,
        settings=_settings(),
        embedding_registry=embedding_registry,
        vector_store=vector_store,
        graph_llm_registry=graph_llm_registry,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=review_conn,
    )

    assert graph_client.synced_terms == graph_terms
