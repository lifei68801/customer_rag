import json

import aiosqlite

from app.api import deps
from app.auth.admin_users_store import create_admin_user, ensure_admin_users_schema
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from app.memory.schema import ensure_schema
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord
from tests.api.conftest import login_client
from tests.settings_factory import build_settings


def _parse_sse_events(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: ")
        events.append(json.loads(block[len("data: ") :]))
    return events


def _final_event(body: str) -> dict:
    events = _parse_sse_events(body)
    final_events = [e for e in events if e.get("type") == "final"]
    assert len(final_events) == 1
    return final_events[0]


_settings = build_settings


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


def _review_conn_override():
    """agent_chat_endpoint 现在直接用 review_conn 查术语表/已确认关系类型/
    已确认实体类型（不再经过已删除的 deps.get_terms 等 Depends，见
    app/api/deps.py 顶部说明），所有测试都要提供一个空 schema 的
    review_conn，语义等价于之前 `dependency_overrides[deps.get_terms] =
    lambda: []`——这几个测试都不关心具体的术语/schema 内容，只关心
    Planner/静态路径怎么选、SSE 事件怎么推送。ontology schema 也要建
    （tenant_relation_types/term_type_relation_allowlist 等表），否则
    list_relation_types/list_term_types 会报 "no such table"。

    /agent/chat 装上认证门之后这个连接还要装下 admin_users：登录和每个
    请求的会话校验都查它。连接因此改成惰性创建、同一个测试内复用同一个
    实例（闭包写法见 test_session_routes.py），否则登录写进去的账号下一个
    请求就查不到了。member-t1 绑租户 t1，正是 _FAKE_RECORDS 挂的那个租户。
    """
    state: dict[str, aiosqlite.Connection] = {}

    async def _get() -> aiosqlite.Connection:
        if "conn" not in state:
            conn = await aiosqlite.connect(":memory:")
            await ensure_terms_schema(conn)
            await ensure_term_edits_schema(conn)
            await ensure_ontology_schema(conn)
            await ensure_admin_users_schema(conn)
            await create_admin_user(
                conn,
                username="member-t1",
                password="password1",
                role="member",
                tenant_id="t1",
            )
            state["conn"] = conn
        return state["conn"]

    return _get


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
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = login_client("member-t1")
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
    payload = _final_event(body)
    assert payload["text"] == "按资料所述，重启路由器即可解决。"
    assert payload["used_sources"] == ["faq/network.md"]
    assert payload.get("audio_segments_base64") is None


class StreamingFakeLLMProvider:
    def __init__(self, chunks: list[str], *, second_response: str = '{"is_safe": true}') -> None:
        self._chunks = chunks
        self._second_response = second_response

    async def stream_complete(self, request: ProviderRequest):
        for chunk in self._chunks:
            yield chunk

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        # OutputSafety 的 semantic_safety_review 仍然会调一次非流式 complete()
        return ProviderResult(text=self._second_response)


def test_agent_chat_streams_delta_events_before_the_final_event():
    import asyncio

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        deps.DEFAULT_LLM_PROVIDER_NAME,
        StreamingFakeLLMProvider(["重启路由器", "即可解决。"]),
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
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = login_client("member-t1")
        with client.stream(
            "POST",
            "/agent/chat",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
        ) as response:
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    events = _parse_sse_events(body)
    delta_events = [e for e in events if e["type"] == "delta"]
    final_events = [e for e in events if e["type"] == "final"]

    assert delta_events == [{"type": "delta", "text": "重启路由器即可解决。"}]
    assert len(final_events) == 1
    assert final_events[0]["text"] == "重启路由器即可解决。"
    # delta 必须先于 final 到达（客户端要能边收边展示）
    assert events.index(delta_events[0]) < events.index(final_events[0])


def test_agent_chat_streams_audio_events_for_voice_requests_when_provider_streams():
    import asyncio

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        deps.DEFAULT_LLM_PROVIDER_NAME,
        StreamingFakeLLMProvider(["重启路由器即可解决。"]),
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
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: FakeTTSProvider()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = login_client("member-t1")
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

    events = _parse_sse_events(body)
    # 语音请求走音频流式合成，不应该出现文字 delta 事件
    assert all(e["type"] != "delta" for e in events)

    audio_events = [e for e in events if e["type"] == "audio"]
    final_events = [e for e in events if e["type"] == "final"]
    assert len(audio_events) == 1
    assert len(final_events) == 1
    # audio 必须先于 final 到达（客户端要能边收边播放）
    assert events.index(audio_events[0]) < events.index(final_events[0])

    import base64

    expected_audio_base64 = base64.b64encode(
        "audio:重启路由器即可解决。".encode("utf-8")
    ).decode("ascii")
    assert audio_events[0]["audio_base64"] == expected_audio_base64
    # final 事件里的 audio_segments_base64 应该汇总了流式阶段已经合成过的
    # 音频，而不是重新合成一遍
    assert final_events[0]["audio_segments_base64"] == [expected_audio_base64]


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
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: FakeTTSProvider()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = login_client("member-t1")
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

    payload = _final_event(body)
    assert payload["audio_segments_base64"]
    assert len(payload["audio_segments_base64"]) >= 1


class ScriptedLLMProvider:
    def __init__(self, responses: list[ProviderResult]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return self._responses.pop(0)


def _empty_dependency_overrides(*, llm_provider, settings, voice_response: bool = False):
    """空知识库场景：静态路径必然因检索为空触发 Fallback（固定话术，不调 LLM
    做回答），Planner 路径可以直接不调工具、用 LLM 的文本直接作答——用这个差异
    作为"到底走了哪条路径"的判定依据，不需要打桩 build_agent_graph 本身。
    """
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider())
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, llm_provider)
    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = lambda: bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    # memory 关闭：这几个测试只关心 Planner/静态路径怎么选，跟记忆无关，
    # 关掉能避免额外的、跟测试意图无关的 LLM 调用（事实抽取）。
    app.dependency_overrides[deps.get_memory_conn] = lambda: None
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: settings


def test_agent_chat_uses_planner_path_when_enabled_and_not_voice():
    llm_provider = ScriptedLLMProvider(
        [
            ProviderResult(text="这是通用问题，无需检索资料。"),  # Planner 决策：直接作答
            ProviderResult(text='{"is_safe": true}'),  # OutputSafety 语义审查
        ]
    )
    _empty_dependency_overrides(
        llm_provider=llm_provider,
        settings=_settings(agent_enable_autonomous_planning=True),
    )
    try:
        client = login_client("member-t1")
        with client.stream(
            "POST", "/agent/chat", json={"question": "你好", "tenant_id": "t1"}
        ) as response:
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    payload = _final_event(body)
    assert payload["text"] == "这是通用问题，无需检索资料。"


def test_agent_chat_forces_static_path_for_voice_even_when_planner_enabled():
    llm_provider = ScriptedLLMProvider(
        [ProviderResult(text="不应该被用到，静态路径检索为空时不会调用LLM。")]
    )
    _empty_dependency_overrides(
        llm_provider=llm_provider,
        settings=_settings(agent_enable_autonomous_planning=True),
    )
    try:
        client = login_client("member-t1")
        with client.stream(
            "POST",
            "/agent/chat",
            json={"question": "你好", "tenant_id": "t1", "voice_response": True},
        ) as response:
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    payload = _final_event(body)
    assert "人工" in payload["text"] or "转" in payload["text"]


def test_agent_chat_uses_session_tenant_over_request_body_and_gateway_header():
    """租户只认会话。请求体里的 tenant_id 和网关头里的 X-Tenant-Id 都被忽略。

    这条以前叫 uses_gateway_tenant_id_over_request_body，钉的是"网关头压过
    请求体"。身份改从会话取之后优先级变成了会话 > 网关头 > 请求体，所以这里
    把两个"错的租户"同时摆上：会话是 t1，只有 t1 的资料能被检索到。
    """
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
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = login_client("member-t1")
        # 请求体和网关头里的 tenant_id 都是错的，_FAKE_RECORDS 只挂在
        # tenant_id="t1" 下——只有真正用于检索的 tenant_id 来自会话（member-t1
        # 绑的就是 t1）时，才能检索到 faq/network.md。
        with client.stream(
            "POST",
            "/agent/chat",
            json={"question": "网络连不上怎么办？", "tenant_id": "wrong-tenant"},
            headers={"X-Tenant-Id": "another-tenant", "X-Gateway-Secret": "sekret"},
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    payload = _final_event(body)
    assert payload["used_sources"] == ["faq/network.md"]


def test_agent_chat_rejects_wrong_gateway_secret_when_configured():
    """配了 gateway_shared_secret 就必须带有效的网关凭证——哪怕已经登录。

    先登录再请求是这条测试的要害：不登录的话 401 也可能来自会话校验，
    那样这条用例就不再证明网关那道门还在。
    """
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
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = lambda: None
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = login_client("member-t1")
        response = client.post(
            "/agent/chat",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "wrong"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_agent_chat_uses_static_path_when_planner_disabled_by_default():
    llm_provider = ScriptedLLMProvider(
        [ProviderResult(text="不应该被用到，静态路径检索为空时不会调用LLM。")]
    )
    _empty_dependency_overrides(llm_provider=llm_provider, settings=_settings())
    try:
        client = login_client("member-t1")
        with client.stream(
            "POST", "/agent/chat", json={"question": "你好", "tenant_id": "t1"}
        ) as response:
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    payload = _final_event(body)
    assert "人工" in payload["text"] or "转" in payload["text"]


def test_agent_chat_emits_tool_status_event_when_planner_calls_a_tool():
    from app.providers.base import ProviderStreamChunk, ToolCall

    class ScriptedStreamingLLMProvider:
        def __init__(self, rounds):
            self._rounds = list(rounds)

        async def complete(self, request):
            raise NotImplementedError("此 Fake 只用于测试流式路径")

        async def stream_complete_with_tools(self, request):
            for chunk in self._rounds.pop(0):
                yield chunk

    import asyncio

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        deps.DEFAULT_LLM_PROVIDER_NAME,
        ScriptedStreamingLLMProvider(
            [
                [
                    ProviderStreamChunk(
                        tool_calls=[
                            ToolCall(id="call_1", name="vector_search_tool", arguments='{"query": "网络连不上怎么办"}')
                        ]
                    )
                ],
                [ProviderStreamChunk(text="重启路由器即可解决。")],
            ]
        ),
    )
    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override()
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = lambda: None
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        agent_enable_autonomous_planning=True
    )
    try:
        client = login_client("member-t1")
        with client.stream(
            "POST",
            "/agent/chat",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
        ) as response:
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    events = _parse_sse_events(body)
    event_types = [e["type"] for e in events]
    assert "tool_status" in event_types
    tool_status_event = next(e for e in events if e["type"] == "tool_status")
    assert tool_status_event["text"] == "正在查询相关信息..."
    assert event_types.index("tool_status") < len(event_types) - 1  # 不是最后一个事件
    assert event_types[-1] == "final"
