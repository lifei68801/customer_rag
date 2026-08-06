import aiosqlite

from app.agent.graph import build_agent_graph
from app.memory.consolidation_queue import list_pending_jobs, process_pending_jobs
from app.memory.memory_store import list_active_memory_items, upsert_memory_item
from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn, get_recent_turns
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
            tenant_id="t1",
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

    result = await graph.ainvoke({"question": "网络连不上怎么办？", "tenant_id": "t1"})

    assert result["final_text"] == "重启路由器即可解决。"


async def test_query_rewrite_receives_recent_conversation_turns_as_context():
    # 客服口语化提问常有指代（"这个报错"），改写需要看到近期对话轮次
    # 才能补全指代，不能只看孤立的一句话。
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await append_turn(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        role="user",
        content="我遇到了E502错误",
    )
    await append_turn(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        role="assistant",
        content="E502是网关超时错误。",
    )

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                '{"is_correction": false}',  # correction_check_node 的纠错意图检测
                '{"start": null, "end": null, "confidence": 0}',  # memory_recall_node 的 resolve_time_window，问题里没有时间表达式
                "错误码E502 网关超时",  # query 改写结果
                "重启路由器即可解决。",  # responder 的回答
                '{"is_safe": true}',  # OutputSafety 语义审查
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
        query_rewrite_enabled=True,
        memory_conn=conn,
    )

    await graph.ainvoke(
        {
            "question": "这个报错怎么解决",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    assert len(llm_provider.requests) >= 2
    rewrite_request = llm_provider.requests[2]
    contents = [message.get("content") for message in rewrite_request.messages]
    assert "我遇到了E502错误" in contents


async def test_memory_enabled_saves_turn_and_injects_context():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await upsert_memory_item(
        conn,
        memory_id="m1",
        tenant_id="t1",
        user_id="u1",
        text="客户使用企业版套餐",
        embedding=[1.0, 0.0],
    )

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                '{"is_correction": false}',  # correction_check_node 的纠错意图检测
                '{"start": null, "end": null, "confidence": 0}',  # memory_recall_node 的 resolve_time_window，问题里没有时间表达式
                "重启路由器即可解决。",  # responder 的回答
                '{"facts":[]}',  # OutputSafety 语义审查（无 is_safe 字段时默认放行未审查）
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
        {
            "question": "网络连不上怎么办？",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    assert result["final_text"] == "重启路由器即可解决。"

    responder_request = llm_provider.requests[2]
    assert any(
        "客户使用企业版套餐" in m["content"]
        for m in responder_request.messages
        if m["role"] == "system"
    )

    turns = await get_recent_turns(conn, tenant_id="t1", session_id="s1", limit=10)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["content"] == "重启路由器即可解决。"


async def test_memory_enabled_enqueues_consolidation_job_without_blocking_response():
    """graph.ainvoke() 返回后 consolidation 应该只是入队（pending），还没有
    真正执行事实抽取/冲突决策——那部分由独立的 worker 异步处理，不阻塞
    本轮响应（见 docs/ARCHITECTURE.md §6.2）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                '{"is_correction": false}',  # correction_check_node 的纠错意图检测
                '{"start": null, "end": null, "confidence": 0}',  # memory_recall_node 的 resolve_time_window，问题里没有时间表达式
                "重启路由器即可解决。",  # responder 的回答
                '{"is_safe": true}',  # OutputSafety 语义审查
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

    await graph.ainvoke(
        {
            "question": "网络连不上怎么办？",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    # 只入队，不应该触发事实抽取——上面只准备了纠错意图检测+resolve_time_window+
    # responder+语义审查四个脚本响应，如果 consolidation 同步跑了，第五次
    # LLM 调用会因为脚本响应耗尽而报错，测试本身就会失败。
    pending = await list_pending_jobs(conn)
    assert len(pending) == 1
    assert pending[0]["user_input"] == "网络连不上怎么办？"
    assert pending[0]["assistant_output"] == "重启路由器即可解决。"

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert items == []


async def test_memory_enabled_stores_embedding_for_newly_added_facts_after_worker_drains_queue():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                '{"is_correction": false}',  # correction_check_node 的纠错意图检测
                '{"start": null, "end": null, "confidence": 0}',  # memory_recall_node 的 resolve_time_window，问题里没有时间表达式
                "重启路由器即可解决。",  # responder 的回答
                '{"is_safe": true}',  # OutputSafety 语义审查
                '{"facts": ["客户使用企业版套餐"]}',  # 事实抽取（worker 处理阶段）
                '{"actions": [{"event": "ADD", "target_memory_id": "", '
                '"text": "客户使用企业版套餐", "reason": "首次提及"}]}',  # 冲突决策
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

    await graph.ainvoke(
        {
            "question": "网络连不上怎么办？",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    processed = await process_pending_jobs(
        conn,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
    )
    assert processed == 1

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert len(items) == 1
    assert items[0]["embedding"] == [1.0, 0.0]
    assert items[0]["embedding"] == [1.0, 0.0]


async def test_memory_recall_injects_structured_history_for_time_bearing_question():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await append_turn(
        conn, tenant_id="t1", session_id="s0", user_id="u1",
        role="user", content="错误码E502网关超时怎么解决",
    )
    # 手动把 created_at 改到"昨天"，确保能被 resolve_time_window 解析出的
    # "昨天"窗口命中（append_turn 本身用 datetime('now') 默认值，测试运行
    # 时刻就是"今天"，这里直接改成昨天，不依赖具体的墙钟时间）。
    await conn.execute(
        "UPDATE conversation_turns SET created_at = datetime('now', '-1 day') "
        "WHERE session_id = 's0'"
    )
    await conn.commit()

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                '{"is_correction": false}',  # correction_check_node 的纠错意图检测
                '{"start": null, "end": null, "confidence": 0}',  # resolve_time_window 的LLM调用故意给低置信度，回退规则引擎判"昨天"
                "重启路由器即可解决。",  # responder
                '{"is_safe": true}',  # OutputSafety 语义审查
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

    await graph.ainvoke(
        {
            "question": "昨天那个E502网关超时问题解决了吗",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    # 请求顺序：correction_check(0) -> memory_recall 的 resolve_time_window(1)
    # -> responder(2) -> output_safety 的语义审查(3)
    assert len(llm_provider.requests) >= 3
    responder_request = llm_provider.requests[2]
    all_content = " ".join(
        m.get("content", "") for m in responder_request.messages
    )
    assert "E502网关超时怎么解决" in all_content


async def test_memory_recall_stays_noop_when_question_has_no_time_expression():
    # 问题里没有可解析的时间表达式时，structured recall 必须完全是 no-op：
    # 不额外拼接系统消息，行为和接入前一致。
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await append_turn(
        conn, tenant_id="t1", session_id="s0", user_id="u1",
        role="user", content="错误码E502网关超时怎么解决",
    )

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                '{"is_correction": false}',  # correction_check_node 的纠错意图检测
                '{"start": null, "end": null, "confidence": 0}',  # resolve_time_window 的LLM调用，问题里没有时间表达式，规则引擎也不命中
                "重启路由器即可解决。",  # responder
                '{"is_safe": true}',  # OutputSafety 语义审查
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

    await graph.ainvoke(
        {
            "question": "网络连不上怎么办？",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    responder_request = llm_provider.requests[2]
    all_content = " ".join(
        m.get("content", "") for m in responder_request.messages
    )
    assert "E502网关超时怎么解决" not in all_content
