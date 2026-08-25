from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.agent.graph import build_agent_graph
from app.agent.tool_registry import discover_tools
from app.memory.consolidation_queue import list_pending_jobs, process_pending_jobs
from app.memory.memory_store import list_active_memory_items, upsert_memory_item
from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn, get_recent_turns
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord

# build_agent_graph 的 tool_registry 现在是必填项（不给默认值），本文件
# 测试都走 memory_conn 打开时的确定性路径，从不实际使用 tool_registry，
# 这里统一构造一次真实注册表传给每个调用点。
_TOOL_REGISTRY = discover_tools(Path(__file__).resolve().parents[2] / "app" / "agent" / "tools")


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


class DispatchingLLMProvider:
    """按请求内容路由到对应响应，而不是假设 LLM 调用严格按位置发生。

    correction_check 与 clarification_check->term_guard->memory_recall 这两
    条分支自 2026-08-12 起并行执行（见 app/agent/graph.py），且
    correction_check 内部还会先过一道规则前置过滤、不像纠正的问题直接跳过
    LLM 调用（app/memory/correction_intent.py）——同一轮里到底会发生哪几次
    LLM 调用、谁先谁后不再是写死的顺序，用 ScriptedLLMProvider 那种位置
    弹队列的写法在这两种改动下都不稳定。按请求消息里出现的关键词（各调用
    点的系统提示词彼此不同，足够区分是谁发起的）匹配到对应的响应队列。
    """

    def __init__(self, rules: list[tuple[str, list[str]]]) -> None:
        self._rules = [(key, list(responses)) for key, responses in rules]
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        haystack = " ".join(m.get("content") or "" for m in request.messages)
        for key, responses in self._rules:
            if key in haystack and responses:
                return ProviderResult(text=responses.pop(0))
        raise AssertionError(
            f"DispatchingLLMProvider 没有匹配到任何规则（或规则响应已耗尽）："
            f"{haystack[:200]!r}"
        )


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


async def _build_dependencies_dispatching(rules: list[tuple[str, list[str]]]):
    """同 _build_dependencies，但 LLM 用 DispatchingLLMProvider——用于图里
    多次 LLM 调用不再有确定先后顺序的测试场景（并行分支/规则前置过滤后）。
    """
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

    llm_provider = DispatchingLLMProvider(rules)
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
        tool_registry=_TOOL_REGISTRY,
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
        tool_registry=_TOOL_REGISTRY,
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
    rewrite_request = llm_provider.requests[1]
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

    # "网络连不上怎么办？"不含任何纠正类线索，correction_check 的规则前置
    # 过滤会直接跳过这次 LLM 调用（不需要为它准备响应）。
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies_dispatching(
            [
                ("根据以下资料回答问题", ["重启路由器即可解决。"]),
                # 无 is_safe 字段时默认放行未审查
                ("语义级安全审查员", ['{"facts":[]}']),
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
        tool_registry=_TOOL_REGISTRY,
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

    responder_requests = [
        req for req in llm_provider.requests
        if "根据以下资料回答问题" in req.messages[-1]["content"]
    ]
    assert len(responder_requests) == 1
    assert any(
        "客户使用企业版套餐" in m["content"]
        for m in responder_requests[0].messages
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
        await _build_dependencies_dispatching(
            [
                ("根据以下资料回答问题", ["重启路由器即可解决。"]),
                ("语义级安全审查员", ['{"is_safe": true}']),
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
        tool_registry=_TOOL_REGISTRY,
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

    # 只入队，不应该触发事实抽取——上面只准备了 responder+语义审查两条规则，
    # 没给事实抽取/冲突决策准备响应，如果 consolidation 同步跑了，
    # DispatchingLLMProvider 会因为找不到匹配规则而报错，测试本身就会失败。
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
        await _build_dependencies_dispatching(
            [
                ("根据以下资料回答问题", ["重启路由器即可解决。"]),
                ("语义级安全审查员", ['{"is_safe": true}']),
                # consolidation worker 处理阶段（graph.ainvoke() 返回之后才跑，
                # 严格顺序发生，不受并行分支影响）：
                ("稍后再自己尝试", ['{"is_delay": false}']),  # detect_delay_intent
                ("长期记忆事实抽取器", ['{"facts": ["客户使用企业版套餐"]}']),
                (
                    "记忆冲突决策器",
                    [
                        '{"actions": [{"event": "ADD", "target_memory_id": "", '
                        '"text": "客户使用企业版套餐", "reason": "首次提及"}]}'
                    ],
                ),
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
        tool_registry=_TOOL_REGISTRY,
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
    # 手动把 created_at 改到"昨天中午"，确保能被 resolve_time_window 解析出
    # 的"昨天"窗口命中。resolve_time_window 产出的窗口边界是 naive 本地
    # 时间（见 app/agent/graph.py 的 reference_time=datetime.now()），而
    # conversation_turns.created_at 存的是 UTC（query_turns_in_window 会把
    # 窗口边界 astimezone 成 UTC 再比较，Finding 1 的修复）。这里不能直接用
    # SQLite `datetime('now', '-1 day')`（那是 UTC 的"昨天"，不是本地的
    # "昨天"）——在 UTC+8 这类时区上，本地凌晨 0-8 点运行测试时，UTC 的
    # "昨天"和本地的"昨天"窗口对不上，会导致测试间歇性失败。改成显式用本地
    # 昨天中午（任何墙钟时刻减一天再钉在正午，都稳稳落在本地"昨天"这一天
    # 的窗口正中间，不会因为窗口边界的时区换算而跑到相邻的一天），再显式
    # 换算成 UTC 字符串写入，测试结果就不再依赖运行测试时的具体墙钟时间。
    local_yesterday_noon = (datetime.now() - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    utc_created_at = local_yesterday_noon.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await conn.execute(
        "UPDATE conversation_turns SET created_at = ? WHERE session_id = 's0'",
        (utc_created_at,),
    )
    await conn.commit()

    # 问题不含任何纠正类线索，correction_check 的规则前置过滤会直接跳过这次
    # LLM 调用；resolve_time_window 故意给低置信度，回退规则引擎判"昨天"。
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies_dispatching(
            [
                ("时间表达式解析器", ['{"start": null, "end": null, "confidence": 0}']),
                ("根据以下资料回答问题", ["重启路由器即可解决。"]),
                ("语义级安全审查员", ['{"is_safe": true}']),
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
        tool_registry=_TOOL_REGISTRY,
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

    responder_requests = [
        req for req in llm_provider.requests
        if "根据以下资料回答问题" in req.messages[-1]["content"]
    ]
    assert len(responder_requests) == 1
    all_content = " ".join(
        m.get("content", "") for m in responder_requests[0].messages
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
        tool_registry=_TOOL_REGISTRY,
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

    responder_request = llm_provider.requests[1]
    all_content = " ".join(
        m.get("content", "") for m in responder_request.messages
    )
    assert "E502网关超时怎么解决" not in all_content


async def test_uses_injected_session_window_store_instead_of_direct_sql():
    from app.memory.session_window_store import SessionWindowStore

    class RecordingSessionWindowStore:
        def __init__(self) -> None:
            self.appended: list[dict] = []

        async def append_turn(self, *, tenant_id, session_id, user_id, role, content):
            self.appended.append(
                {"tenant_id": tenant_id, "session_id": session_id, "role": role, "content": content}
            )

        async def get_recent_turns(self, *, tenant_id, session_id, limit):
            return [
                {"role": item["role"], "content": item["content"]}
                for item in self.appended
                if item["tenant_id"] == tenant_id and item["session_id"] == session_id
            ][-limit:]

    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    session_window_store = RecordingSessionWindowStore()

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(["重启路由器即可解决。", '{"facts":[]}'])
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tool_registry=_TOOL_REGISTRY,
        query_rewrite_enabled=False,
        memory_conn=conn,
        session_window_store=session_window_store,
    )

    await graph.ainvoke(
        {
            "question": "网络连不上怎么办？",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    assert len(session_window_store.appended) == 2
    assert session_window_store.appended[0]["role"] == "user"
    assert session_window_store.appended[1]["role"] == "assistant"
