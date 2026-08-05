import aiosqlite

from app.agent.graph import build_agent_graph
from app.memory.memory_store import upsert_memory_item
from app.memory.schema import ensure_schema
from app.memory.session_window import get_recent_turns
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
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        text = self._responses.pop(0) if self._responses else '{"facts":[]}'
        return ProviderResult(text=text)


async def _build_dependencies(llm_responses: list[str]):
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    vector_store = InMemoryVectorStore()
    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            metadata={},
        )
    ]
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_provider = ScriptedLLMProvider(llm_responses)
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    return embedding_registry, vector_store, bm25_index, llm_registry, llm_provider


async def test_memory_disabled_by_default_matches_stage4_behavior():
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(["重启路由器即可解决。"])
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    result = await graph.ainvoke({"question": "网络连不上怎么办？"})

    assert result["final_text"] == "重启路由器即可解决。"


async def test_memory_enabled_saves_turn_and_injects_context():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await upsert_memory_item(
        conn, memory_id="m1", user_id="u1", text="客户使用企业版套餐"
    )

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                "重启路由器即可解决。",  # responder 的回答
                '{"facts":[]}',  # 事实抽取（对话后置处理）
            ]
        )
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        memory_conn=conn,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "session_id": "s1", "user_id": "u1"}
    )

    assert result["final_text"] == "重启路由器即可解决。"

    responder_request = llm_provider.requests[0]
    assert any(
        "客户使用企业版套餐" in m["content"]
        for m in responder_request.messages
        if m["role"] == "system"
    )

    turns = await get_recent_turns(conn, session_id="s1", limit=10)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["content"] == "重启路由器即可解决。"
