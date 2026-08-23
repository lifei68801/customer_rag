import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.config.settings import Settings
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
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
        return ProviderResult(text="按资料所述，重启路由器即可解决。")


_FAKE_RECORDS = [
    VectorRecord(
        id="faq/network.md",
        vector=[1.0, 0.0],
        text="网络断开时，请先重启路由器。",
        tenant_id="t1",
        metadata={},
    )
]


async def _fake_vector_store() -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    await store.upsert(_FAKE_RECORDS)
    return store


def _fake_bm25_index() -> BM25Index:
    index = BM25Index()
    index.index(_FAKE_RECORDS)
    return index


async def _override_get_review_conn() -> aiosqlite.Connection:
    # qa_endpoint 现在直接用 review_conn 查术语表（不再经过已删除的
    # deps.get_terms，见 app/api/deps.py 顶部说明）——空 schema 的
    # review_conn 语义等价于之前 `dependency_overrides[deps.get_terms] =
    # lambda: []`，这几个测试都不关心具体术语内容。
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    return conn


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        gateway_shared_secret=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_qa_endpoint_returns_answer_and_used_sources():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    import asyncio

    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _override_get_review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    # gateway_shared_secret 显式钉死为 None：不 override 的话 get_settings
    # 会读真实环境变量/.env，一旦开发者本机或 .env 配置了
    # CUSTOMER_RAG_GATEWAY_SHARED_SECRET（正是这个安全修复要促使运营者去做
    # 的事），这条与网关鉴权无关的测试会意外因缺少网关凭证被 401 拒绝。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        response = client.post(
            "/qa", json={"question": "网络连不上怎么办？", "tenant_id": "t1"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "按资料所述，重启路由器即可解决。"
    assert body["used_sources"] == ["faq/network.md"]


def test_qa_endpoint_does_not_leak_another_tenants_sources():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    import asyncio

    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _override_get_review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    # gateway_shared_secret 显式钉死为 None，理由同上一条测试。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        response = client.post(
            "/qa", json={"question": "网络连不上怎么办？", "tenant_id": "t2"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["used_sources"] == []


def test_qa_endpoint_uses_gateway_tenant_id_over_request_body():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    import asyncio

    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _override_get_review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/qa",
            json={"question": "网络连不上怎么办？", "tenant_id": "wrong-tenant"},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "sekret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["used_sources"] == ["faq/network.md"]


def test_qa_endpoint_rejects_wrong_gateway_secret_when_configured():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_bm25_index] = lambda: BM25Index()
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _override_get_review_conn
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/qa",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
