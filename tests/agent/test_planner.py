import asyncio
import json

from app.agent.planner import (
    route_after_planner,
    run_planner_turn,
    run_planner_turn_streaming,
    run_tool_calls,
)
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.providers.base import (
    ProviderCapability,
    ProviderRequest,
    ProviderResult,
    ProviderStreamChunk,
    ToolCall,
)
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord
from app.safety.rules import LITE_SAFETY_FALLBACK_SENTENCE


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class ScriptedLLMProvider:
    def __init__(self, responses: list[ProviderResult]) -> None:
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        return self._responses.pop(0)


def _build_store_and_index(tenant_id: str = "t1"):
    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时请先重启路由器。",
            tenant_id=tenant_id,
            metadata={},
        ),
        VectorRecord(
            id="faq/other-tenant.md",
            vector=[1.0, 0.0],
            text="属于别的租户的资料",
            tenant_id="t2",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()
    return records, vector_store, bm25_index


async def _seed_store(records, vector_store, bm25_index):
    await vector_store.upsert(records)
    bm25_index.index(records)


def _embedding_registry() -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register("fake-embedding", FakeEmbeddingProvider())
    return registry


async def test_run_planner_turn_returns_pending_tool_calls_when_llm_requests_a_tool():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="vector_search_tool",
                            arguments='{"query": "网络连不上怎么办"}',
                        )
                    ],
                )
            ]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "网络连不上怎么办？"}],
        "tool_call_round": 0,
    }

    update = await run_planner_turn(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
    )

    assert update["pending_tool_calls"] == [
        {"id": "call_1", "name": "vector_search_tool", "arguments": '{"query": "网络连不上怎么办"}'}
    ]
    assert update.get("planner_gave_up") in (None, False)
    # 助手请求工具这条消息应该被追加进对话历史，供下一轮 planner 看到上下文
    assert update["planner_messages"][-1]["role"] == "assistant"
    assert update["planner_messages"][-1]["tool_calls"][0]["id"] == "call_1"


async def test_run_planner_turn_returns_answer_when_llm_stops_calling_tools():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider([ProviderResult(text="重启路由器即可解决。")]),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "网络连不上怎么办？"}],
        "tool_call_round": 1,
    }

    update = await run_planner_turn(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
    )

    assert update["answer_text"] == "重启路由器即可解决。"
    assert update["planner_gave_up"] is False
    assert "pending_tool_calls" not in update


async def test_run_planner_turn_gives_up_when_final_answer_attempt_also_returns_empty_text():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(id="call_x", name="vector_search_tool", arguments="{}")
                    ],
                ),
                ProviderResult(text=""),
            ]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
    }

    update = await run_planner_turn(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
    )

    assert update == {"planner_gave_up": True}


async def test_run_planner_turn_final_answer_attempt_succeeds_when_rounds_exhausted():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(id="call_x", name="vector_search_tool", arguments="{}")
                    ],
                ),
                ProviderResult(text="根据目前查到的信息，Cola 有 992 个订单。"),
            ]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
    }

    update = await run_planner_turn(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
    )

    assert update["planner_gave_up"] is False
    assert update["answer_text"] == "根据目前查到的信息，Cola 有 992 个订单。"
    assert "pending_tool_calls" not in update
    assert update["planner_messages"][-1] == {
        "role": "assistant",
        "content": "根据目前查到的信息，Cola 有 992 个订单。",
    }


async def test_run_planner_turn_final_answer_attempt_does_not_pass_tools():
    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider(
        [
            ProviderResult(
                text="",
                tool_calls=[ToolCall(id="call_x", name="vector_search_tool", arguments="{}")],
            ),
            ProviderResult(text="总结性回答。"),
        ]
    )
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
    }

    await run_planner_turn(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
    )

    assert provider.requests[1].tools is None


async def test_run_tool_calls_executes_vector_search_and_scopes_to_state_tenant():
    records, vector_store, bm25_index = _build_store_and_index(tenant_id="t1")
    await _seed_store(records, vector_store, bm25_index)
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([])
    )

    state = {
        "tenant_id": "t1",
        "planner_messages": [{"role": "user", "content": "网络连不上怎么办？"}],
        # 恶意/异常情况下 arguments 里混入了别的 tenant_id，执行时必须被忽略
        "pending_tool_calls": [
            {
                "id": "call_1",
                "name": "vector_search_tool",
                "arguments": '{"query": "网络连不上怎么办", "tenant_id": "t2"}',
            }
        ],
    }

    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    assert update["pending_tool_calls"] == []
    assert [r.id for r in update["retrieved_records"]] == ["faq/network.md"]
    tool_message = update["planner_messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"
    assert "faq/network.md" in tool_message["content"]
    assert "faq/other-tenant.md" not in tool_message["content"]


_TERMS = [
    Term(
        tenant_id="t1",
        node_key="示例错误码E502",
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
    )
]


class FakeGraphClientForStructuredQuery:
    def __init__(self, *, rows=None, total_count=None) -> None:
        self._rows = rows if rows is not None else []
        self._total_count = total_count if total_count is not None else len(self._rows)
        self.queried_tenant_ids: list[str] = []

    async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
        self.queried_tenant_ids.append(tenant_id)
        return {"rows": self._rows, "total_count": self._total_count}


async def test_run_tool_calls_executes_structured_filter_query_tool_with_name_anchor():
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"anchor": {"name": "网关超时示例"}}',
            }
        ],
    }

    graph_client = FakeGraphClientForStructuredQuery(rows=[{
        "standard_name": "示例错误码E502", "node_key": "示例错误码E502", "term_type": "error_code",
        "all_properties": {},
    }])
    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=_TERMS,
        graph_client=graph_client,
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    tool_message = update["planner_messages"][-1]
    assert "示例错误码E502" in tool_message["content"]
    assert graph_client.queried_tenant_ids == ["t1"]


async def test_run_tool_calls_annotates_expand_neighbors_with_association():
    """expand 返回的 neighbors 要按 hops 标注 association 文案——原
    graph_query_tool 分支的既有行为，迁移到 structured_filter_query_tool。"""
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"anchor": {"name": "网关超时示例"}, "expand": {"hops": 2}}',
            }
        ],
    }

    graph_client = FakeGraphClientForStructuredQuery(rows=[{
        "standard_name": "示例错误码E502", "node_key": "示例错误码E502", "term_type": "error_code",
        "all_properties": {},
        "neighbors": [{"related_name": "示例登录模块", "relation_type": "RELATED_TO", "hops": 2}],
    }])
    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=_TERMS,
        graph_client=graph_client,
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    tool_message = update["planner_messages"][-1]
    parsed = json.loads(tool_message["content"])
    assert parsed["anchors"][0]["neighbors"][0]["association"] == "间接关联（经过 2 跳）"


def test_tool_schemas_no_longer_include_graph_query_tool():
    from app.agent.planner import _TOOL_SCHEMAS
    names = [s["function"]["name"] for s in _TOOL_SCHEMAS]
    assert "graph_query_tool" not in names
    assert "structured_filter_query_tool" in names


async def test_run_tool_calls_reports_error_for_malformed_arguments_without_crashing():
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_3", "name": "vector_search_tool", "arguments": "不是合法JSON"}
        ],
    }

    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
    )

    tool_message = update["planner_messages"][-1]
    assert tool_message["role"] == "tool"
    parsed = json.loads(tool_message["content"])
    assert "error" in parsed


def test_route_after_planner_goes_to_tool_call_when_pending():
    state = {"pending_tool_calls": [{"id": "x", "name": "vector_search_tool", "arguments": "{}"}]}
    assert route_after_planner(state) == "tool_call"


def test_route_after_planner_goes_to_fallback_when_gave_up():
    state = {"planner_gave_up": True}
    assert route_after_planner(state) == "fallback"


def test_route_after_planner_goes_to_responder_when_answer_ready():
    state = {"answer_text": "答案", "planner_gave_up": False}
    assert route_after_planner(state) == "responder"


async def test_run_tool_calls_executes_multiple_tools_concurrently(monkeypatch):
    """同一轮请求了两个工具时，两次 _dispatch_tool_call 应该并发执行，
    不是排队顺序执行——用两个互等的 asyncio.Event 证明。"""
    import app.agent.planner as planner_module

    started = {"call_1": asyncio.Event(), "call_2": asyncio.Event()}

    async def fake_dispatch_tool_call(name, arguments, **kwargs):
        call_id = arguments["call_id"]
        started[call_id].set()
        other = "call_2" if call_id == "call_1" else "call_1"
        await asyncio.wait_for(started[other].wait(), timeout=5)
        return f'{{"ok": "{call_id}"}}', []

    monkeypatch.setattr(planner_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_1", "name": "vector_search_tool", "arguments": '{"call_id": "call_1"}'},
            {"id": "call_2", "name": "structured_filter_query_tool", "arguments": '{"call_id": "call_2"}'},
        ],
    }

    update = await run_tool_calls(
        state,
        embedding_registry=_embedding_registry(),
        embedding_provider_name="fake-embedding",
        vector_store=InMemoryVectorStore(),
        bm25_index=BM25Index(),
        llm_registry=ProviderRegistry(),
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    contents_by_call_id = {r["tool_call_id"]: r["content"] for r in update["tool_results"]}
    assert contents_by_call_id["call_1"] == '{"ok": "call_1"}'
    assert contents_by_call_id["call_2"] == '{"ok": "call_2"}'
    # 顺序必须和 pending_tool_calls 原始顺序一致，不依赖谁先完成
    assert [r["tool_call_id"] for r in update["tool_results"]] == ["call_1", "call_2"]


async def test_run_tool_calls_propagates_exception_from_tool_dispatch(monkeypatch):
    """当某个工具调用抛异常时，run_tool_calls() 应该立刻抛出该异常，
    不因为用了 asyncio.gather(return_exceptions=True) 就吞了它。"""
    import app.agent.planner as planner_module

    async def fake_dispatch_tool_call(name, arguments, **kwargs):
        if arguments.get("fail"):
            raise ValueError("模拟工具调用失败")
        return '{"ok": "success"}', []

    monkeypatch.setattr(planner_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_1", "name": "vector_search_tool", "arguments": '{}'},
            {"id": "call_2", "name": "structured_filter_query_tool", "arguments": '{"fail": true}'},
        ],
    }

    try:
        await run_tool_calls(
            state,
            embedding_registry=_embedding_registry(),
            embedding_provider_name="fake-embedding",
            vector_store=InMemoryVectorStore(),
            bm25_index=BM25Index(),
            llm_registry=ProviderRegistry(),
            llm_provider_name="fake-llm",
        )
        assert False, "应该抛异常"
    except ValueError as e:
        assert "模拟工具调用失败" in str(e)


async def test_dispatch_tool_call_routes_structured_filter_query_tool():
    from app.agent.planner import _dispatch_tool_call

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            raise AssertionError("should not be called")

    content, records = await _dispatch_tool_call(
        "structured_filter_query_tool",
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "standard_name", "operator": "eq", "value": "x"}]},
        tenant_id="muji",
        embedding_registry=None, embedding_provider_name="", vector_store=None, bm25_index=None,
        llm_registry=None, llm_provider_name="", rerank_provider=None, query_rewrite_enabled=False,
        terms=[], graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={},
    )

    assert records == []
    # SKU 不在空的 term_type_schema 里，validate_structured_filter_query 应拒绝，
    # 走结构化错误分支（而不是命中未配置守卫，也不会真的执行图谱查询）。
    parsed = json.loads(content)
    assert "error" in parsed
    assert "term_type" in parsed["error"]


async def test_dispatch_tool_call_reports_unconfigured_when_schema_data_missing():
    from app.agent.planner import _dispatch_tool_call

    content, records = await _dispatch_tool_call(
        "structured_filter_query_tool", {"anchor_term_type": "SKU", "constraints": []},
        tenant_id="muji",
        embedding_registry=None, embedding_provider_name="", vector_store=None, bm25_index=None,
        llm_registry=None, llm_provider_name="", rerank_provider=None, query_rewrite_enabled=False,
        terms=None, graph_client=None, confirmed_relation_types=None, term_type_schema=None,
    )

    assert records == []
    assert "未配置" in content


class ScriptedStreamingLLMProvider:
    """一次注册若干"轮次"的流式响应，每轮是一个 ProviderStreamChunk 列表；
    stream_complete_with_tools 每次调用弹出下一轮，逐个 yield——跟本文件
    已有的 ScriptedLLMProvider（非流式，弹出一个 ProviderResult）是同一
    个"按调用顺序消费预先编排好的脚本"思路，只是这里每轮是一组 chunk
    而不是一个完整结果。"""

    def __init__(self, rounds: list[list[ProviderStreamChunk]]) -> None:
        self._rounds = list(rounds)
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise NotImplementedError("此 Fake 只用于测试流式路径")

    async def stream_complete_with_tools(self, request: ProviderRequest):
        self.requests.append(request)
        for chunk in self._rounds.pop(0):
            yield chunk


async def test_run_planner_turn_streaming_forwards_text_deltas_for_direct_answer():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedStreamingLLMProvider(
            [
                [
                    ProviderStreamChunk(text="重启路由器"),
                    ProviderStreamChunk(text="即可解决。"),
                ]
            ]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "网络连不上怎么办？"}],
        "tool_call_round": 0,
    }
    sent_chunks: list[str] = []
    tool_status_calls = 0

    async def on_answer_chunk(text: str) -> None:
        sent_chunks.append(text)

    async def on_tool_status() -> None:
        nonlocal tool_status_calls
        tool_status_calls += 1

    update = await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    assert update["answer_text"] == "重启路由器即可解决。"
    assert update["planner_gave_up"] is False
    assert sent_chunks == ["重启路由器即可解决。"]
    assert tool_status_calls == 0


async def test_run_planner_turn_streaming_does_not_forward_text_for_pure_tool_call_round():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedStreamingLLMProvider(
            [
                [
                    ProviderStreamChunk(
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="vector_search_tool",
                                arguments='{"query": "网络连不上怎么办"}',
                            )
                        ]
                    )
                ]
            ]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "网络连不上怎么办？"}],
        "tool_call_round": 0,
    }
    sent_chunks: list[str] = []
    tool_status_calls = 0

    async def on_answer_chunk(text: str) -> None:
        sent_chunks.append(text)

    async def on_tool_status() -> None:
        nonlocal tool_status_calls
        tool_status_calls += 1

    update = await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    assert sent_chunks == []
    assert tool_status_calls == 1
    assert update["pending_tool_calls"] == [
        {"id": "call_1", "name": "vector_search_tool", "arguments": '{"query": "网络连不上怎么办"}'}
    ]
    assert update["planner_messages"][-1]["tool_calls"][0]["function"]["name"] == "vector_search_tool"


async def test_run_planner_turn_streaming_replaces_sentence_matching_banned_term():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedStreamingLLMProvider(
            [[ProviderStreamChunk(text="这句话里有敏感词。")]]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "随便问点什么"}],
        "tool_call_round": 0,
    }
    sent_chunks: list[str] = []

    async def on_answer_chunk(text: str) -> None:
        sent_chunks.append(text)

    async def on_tool_status() -> None:
        pass

    update = await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=["敏感词"],
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    assert sent_chunks == [LITE_SAFETY_FALLBACK_SENTENCE]
    assert update["answer_text"] == LITE_SAFETY_FALLBACK_SENTENCE


async def test_run_planner_turn_streaming_preserves_embedded_newline_when_no_substitution():
    """Finding 3 回归测试：markdown 列表这类"句内无终止标点、句间有换行"
    的文本，经过 stream_sentences 按句切分再拼接会把句子之间的换行丢掉
    （"- 重启路由器。\\n- 检查网线。" 变成 "- 重启路由器。- 检查网线。"）。
    没有触发任何安全替换时，answer_text 必须改用原始增量直接拼接，保留
    大模型输出的原始换行。"""
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedStreamingLLMProvider(
            [[ProviderStreamChunk(text="第一行。\n第二行。")]]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "怎么修？"}],
        "tool_call_round": 0,
    }

    async def on_answer_chunk(text: str) -> None:
        pass

    async def on_tool_status() -> None:
        pass

    update = await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    # 按句子拼接会丢失中间的换行（"第一行。第二行。"），原始增量拼接则
    # 保留它——这正是本次修复要验证的行为。
    assert update["answer_text"] == "第一行。\n第二行。"
    assert update["planner_messages"][-1]["content"] == "第一行。\n第二行。"


async def test_run_planner_turn_streaming_uses_joined_sentences_not_raw_text_when_substituted():
    """当某一句被安全规则替换过时，answer_text 必须使用按句子拼接、已经
    做过安全替换的版本，而不是原始增量拼接（那样会把被过滤的敏感词原样
    带回 answer_text/planner_messages，等于没过滤）。"""
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedStreamingLLMProvider(
            [[ProviderStreamChunk(text="这是安全的第一句。这句话里有敏感词。")]]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "随便问点什么"}],
        "tool_call_round": 0,
    }
    sent_chunks: list[str] = []

    async def on_answer_chunk(text: str) -> None:
        sent_chunks.append(text)

    async def on_tool_status() -> None:
        pass

    update = await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=["敏感词"],
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    assert sent_chunks == ["这是安全的第一句。", LITE_SAFETY_FALLBACK_SENTENCE]
    # 原始增量拼接的话会是 "这是安全的第一句。这句话里有敏感词。"，
    # 原样带回被过滤的敏感词——必须不是这个值。
    assert update["answer_text"] == "这是安全的第一句。" + LITE_SAFETY_FALLBACK_SENTENCE
    assert "敏感词" not in update["answer_text"]


async def test_run_planner_turn_streaming_gives_up_when_final_answer_attempt_also_fails():
    llm_registry = ProviderRegistry()
    provider = ScriptedStreamingLLMProvider(
        [
            [
                ProviderStreamChunk(
                    tool_calls=[ToolCall(id="call_1", name="vector_search_tool", arguments="{}")]
                )
            ],
            [ProviderStreamChunk(text="")],
        ]
    )
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
    }
    tool_status_calls = 0

    async def on_answer_chunk(text: str) -> None:
        pass

    async def on_tool_status() -> None:
        nonlocal tool_status_calls
        tool_status_calls += 1

    update = await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    # 证明"最后陈述"重试真的发生过（第二轮脚本被消费、返回空文本导致放弃），
    # 而不是轮次耗尽时的旧短路逻辑直接放弃、根本没调用
    # _run_final_answer_attempt_streaming——旧代码下这个断言会因为
    # len(provider.requests) == 1 而失败。
    assert len(provider.requests) == 2
    assert update == {"planner_gave_up": True}
    assert tool_status_calls == 0


async def test_run_planner_turn_streaming_final_answer_attempt_succeeds_when_rounds_exhausted():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedStreamingLLMProvider(
            [
                [
                    ProviderStreamChunk(
                        text="让我查一下。",
                        tool_calls=[
                            ToolCall(id="call_1", name="vector_search_tool", arguments="{}")
                        ],
                    )
                ],
                [ProviderStreamChunk(text="根据已有信息，答案是992。")],
            ]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
    }
    sent_chunks: list[str] = []
    tool_status_calls = 0

    async def on_answer_chunk(text: str) -> None:
        sent_chunks.append(text)

    async def on_tool_status() -> None:
        nonlocal tool_status_calls
        tool_status_calls += 1

    update = await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    assert update["planner_gave_up"] is False
    assert update["answer_text"] == "根据已有信息，答案是992。"
    assert tool_status_calls == 0
    # 这一轮被拒绝前的叙述文字（"让我查一下。"）没有触发 tool_status，
    # 用户会看到它跟这次总结文字连在一起、无缝过渡，而不是中间被清空。
    assert sent_chunks == ["让我查一下。", "根据已有信息，答案是992。"]
    assert update["streamed_round_texts"] == ["让我查一下。", "根据已有信息，答案是992。"]


async def test_run_planner_turn_streaming_final_answer_attempt_does_not_pass_tools():
    llm_registry = ProviderRegistry()
    provider = ScriptedStreamingLLMProvider(
        [
            [
                ProviderStreamChunk(
                    tool_calls=[ToolCall(id="call_1", name="vector_search_tool", arguments="{}")]
                )
            ],
            [ProviderStreamChunk(text="总结性回答。")],
        ]
    )
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
    }

    async def on_answer_chunk(text: str) -> None:
        pass

    async def on_tool_status() -> None:
        pass

    await run_planner_turn_streaming(
        state,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        max_tool_call_rounds=3,
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    assert provider.requests[1].tools is None
