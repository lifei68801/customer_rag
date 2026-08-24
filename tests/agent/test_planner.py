import asyncio
import json

from app.agent.planner import (
    route_after_planner,
    run_planner_turn,
    run_planner_turn_streaming,
    run_tool_calls,
)
from app.graphrag.ontology import Term
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


async def test_run_planner_turn_gives_up_when_max_rounds_exceeded():
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
                )
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

    assert update["planner_gave_up"] is True
    assert "pending_tool_calls" not in update


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


class FakeGraphClient:
    def __init__(self) -> None:
        self.queried_tenant_ids: list[str] = []
        self.queried_node_keys: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_tenant_ids.append(tenant_id)
        self.queried_node_keys.append(standard_name)
        return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]


async def test_run_tool_calls_executes_graph_query_tool():
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
                "name": "graph_query_tool",
                "arguments": '{"entity_name": "网关超时示例"}',
            }
        ],
    }

    graph_client = FakeGraphClient()
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
    )

    tool_message = update["planner_messages"][-1]
    assert "示例错误码E502" in tool_message["content"]
    assert "示例登录模块" in tool_message["content"]
    assert graph_client.queried_tenant_ids == ["t1"]


async def test_run_tool_calls_passes_entity_type_argument_to_graph_query_tool():
    """LLM 在 tool_call 的 arguments 里传了 entity_type 时，必须原样透传到
    graph_query_tool，用来在两个同名不同类型的术语之间精确消歧——不传
    entity_type，"Coffee" 到底是产品还是类目就没法确定。"""
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    terms = [
        Term(
            tenant_id="t1", node_key="产品:Coffee", standard_name="Coffee",
            aliases=[], term_type="产品",
        ),
        Term(
            tenant_id="t1", node_key="类目:Coffee", standard_name="Coffee",
            aliases=[], term_type="类目",
        ),
    ]
    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "graph_query_tool",
                "arguments": '{"entity_name": "Coffee", "entity_type": "类目"}',
            }
        ],
    }

    graph_client = FakeGraphClient()
    update = await run_tool_calls(
        state,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=terms,
        graph_client=graph_client,
    )

    tool_message = update["planner_messages"][-1]
    parsed = json.loads(tool_message["content"])
    assert parsed["resolved"] is True
    assert parsed["standard_name"] == "Coffee"
    # entity_type="类目" 必须被透传到 graph_query_tool 并用来精确消歧——
    # 查图谱用的 node_key 必须是"类目:Coffee"而不是"产品:Coffee"。
    assert graph_client.queried_node_keys == ["类目:Coffee"]


class FakeGraphClientWithTwoHopRow:
    """返回一条带 hops=2 的子图行，用来验证 planner 给它标注 association 字段。"""

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        return [
            {"related_name": "示例登录模块", "relation_type": "RELATED_TO", "hops": 2}
        ]


async def test_run_tool_calls_annotates_two_hop_subgraph_rows_with_association():
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
                "name": "graph_query_tool",
                "arguments": '{"entity_name": "网关超时示例"}',
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
        terms=_TERMS,
        graph_client=FakeGraphClientWithTwoHopRow(),
    )

    tool_message = update["planner_messages"][-1]
    parsed = json.loads(tool_message["content"])
    assert parsed["subgraph"][0]["association"] == "间接关联（经过 2 跳）"


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
            {"id": "call_2", "name": "graph_query_tool", "arguments": '{"call_id": "call_2"}'},
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
            {"id": "call_2", "name": "graph_query_tool", "arguments": '{"fail": true}'},
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
        async def execute_structured_filter_query(self, args, *, tenant_id):
            return []

    content, records = await _dispatch_tool_call(
        "structured_filter_query_tool",
        {"anchor_term_type": "SKU",
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        tenant_id="muji",
        embedding_registry=None, embedding_provider_name="", vector_store=None, bm25_index=None,
        llm_registry=None, llm_provider_name="", rerank_provider=None, query_rewrite_enabled=False,
        terms=None, graph_client=_FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={},
    )

    assert records == []
    assert "error" in content  # SKU 不在空的 term_type_schema 里，预期走结构化错误分支


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


async def test_run_planner_turn_streaming_gives_up_without_tool_status_when_rounds_exhausted():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedStreamingLLMProvider(
            [
                [
                    ProviderStreamChunk(
                        tool_calls=[
                            ToolCall(id="call_1", name="vector_search_tool", arguments="{}")
                        ]
                    )
                ]
            ]
        ),
    )
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

    assert update == {"planner_gave_up": True}
    assert tool_status_calls == 0
