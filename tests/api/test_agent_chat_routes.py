import json

import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.memory.schema import ensure_schema
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


def test_agent_chat_streams_final_answer_as_sse():
    import asyncio

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )
    vector_store = asyncio.run(_fake_vector_store())

    async def _override_get_memory_conn() -> aiosqlite.Connection:
        # 必须在 FastAPI 实际处理请求的那个事件循环内创建 aiosqlite 连接，
        # 不能像 vector_store 那样提前用 asyncio.run() 在外部建好再传入——
        # aiosqlite.Connection 内部有个绑定到"创建时那个循环"的后台线程，
        # asyncio.run() 一返回该循环就关闭了，之后从 TestClient 的新循环里
        # 使用这个连接会导致回调发不出去，直接死锁（且 CPU 占用是 0，
        # 因为它是在等一个永远不会完成的 future，不是死循环）。
        conn = await aiosqlite.connect(":memory:")
        await ensure_schema(conn)
        return conn

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    try:
        client = TestClient(app)
        with client.stream(
            "POST",
            "/agent/chat",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    assert body.startswith("data: ")
    payload = json.loads(body[len("data: ") :].strip())
    assert payload["text"] == "按资料所述，重启路由器即可解决。"
    assert payload["used_sources"] == ["faq/network.md"]
    assert payload.get("audio_segments_base64") is None


class FakeTTSProvider:
    async def synthesize(self, request):
        from app.providers.tts import TTSResult

        return TTSResult(audio_bytes=f"audio:{request.text}".encode())


def test_agent_chat_synthesizes_voice_when_requested():
    import asyncio

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )
    vector_store = asyncio.run(_fake_vector_store())

    async def _override_get_memory_conn() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        await ensure_schema(conn)
        return conn

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: FakeTTSProvider()
    try:
        client = TestClient(app)
        with client.stream(
            "POST",
            "/agent/chat",
            json={
                "question": "网络连不上怎么办？",
                "tenant_id": "t1",
                "voice_response": True,
            },
        ) as response:
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    payload = json.loads(body[len("data: ") :].strip())
    assert payload["audio_segments_base64"]
    assert len(payload["audio_segments_base64"]) >= 1
