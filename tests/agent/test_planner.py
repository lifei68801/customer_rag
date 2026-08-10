import asyncio
import json

from app.agent.planner import route_after_planner, run_planner_turn, run_tool_calls
from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult, ToolCall
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


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
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
        product_line="示例产品线",
    )
]


class FakeGraphClient:
    def __init__(self) -> None:
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_tenant_ids.append(tenant_id)
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
