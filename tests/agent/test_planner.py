import asyncio
import json

from app.agent.planner import (
    route_after_planner,
    run_planner_turn,
    run_planner_turn_streaming,
    run_tool_calls,
)
from app.agent.tool_registry import ToolContext, ToolManifest, ToolRegistry
from app.agent.tools.structured_filter_query.tool import TOOL as STRUCTURED_FILTER_QUERY_TOOL
from app.agent.tools.vector_search.tool import TOOL as VECTOR_SEARCH_TOOL
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


def _fake_tool_registry(**tools) -> ToolRegistry:
    """tools 是 name -> Tool 实例的映射，直接注册进一个真实的 ToolRegistry
    （复用真实类而不是再造一个 fake registry），manifest 用最简单的占位
    内容，测试不关心 schema 具体形状时用这个够了。"""
    registry = ToolRegistry()
    for name, tool in tools.items():
        registry.register(
            ToolManifest(name=name, description="", parameters_schema={"type": "object", "properties": {}}),
            tool,
        )
    return registry


def _full_tool_registry() -> ToolRegistry:
    """run_planner_turn/run_planner_turn_streaming 系列测试大多不关心
    tools schema 的具体内容，只需要一个包含真实两个工具的注册表即可。"""
    return _fake_tool_registry(
        vector_search_tool=VECTOR_SEARCH_TOOL,
        structured_filter_query_tool=STRUCTURED_FILTER_QUERY_TOOL,
    )


def _context(**overrides) -> ToolContext:
    defaults = dict(
        tenant_id="t1", question="",
        embedding_registry=None, embedding_provider_name="",
        vector_store=None, bm25_index=None,
        llm_registry=None, llm_provider_name="fake-llm",
        rerank_provider=None, query_rewrite_enabled=False,
        terms=[], graph_client=None, confirmed_relation_types=set(),
        term_type_schema={}, allowed_combinations=[],
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


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
        tool_registry=_full_tool_registry(),
    )

    assert update["pending_tool_calls"] == [
        {"id": "call_1", "name": "vector_search_tool", "arguments": '{"query": "网络连不上怎么办"}'}
    ]
    assert update.get("planner_gave_up") in (None, False)
    # 助手请求工具这条消息应该被追加进对话历史，供下一轮 planner 看到上下文
    assert update["planner_messages"][-1]["role"] == "assistant"
    assert update["planner_messages"][-1]["tool_calls"][0]["id"] == "call_1"


async def test_planner_sends_byte_identical_tools_schema_across_rounds():
    """tools 参数必须逐字节保持一致——这是这份设计能兼容 KV cache 的硬约束，
    不能只在某次改动时人工检查一遍，需要一个能长期把关的回归测试。"""
    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider([
        ProviderResult(text="", tool_calls=[
            ToolCall(id="call_1", name="vector_search_tool", arguments='{"query": "x"}'),
        ]),
        ProviderResult(text="最终答案"),
    ])
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    tool_registry = _full_tool_registry()

    state = {"planner_messages": [{"role": "user", "content": "随便问点什么"}], "tool_call_round": 0}
    await run_planner_turn(
        state, llm_registry=llm_registry, llm_provider_name="fake-llm", max_tool_call_rounds=3,
        tool_registry=tool_registry,
    )
    # 每轮结束后立刻拍快照（序列化成字符串），不留到最后才比引用——
    # ScriptedLLMProvider 存的是 ProviderRequest 对象引用，tool_registry.schemas()
    # 每次调用都会重新构造一份新列表，如果内容之间有差异，最后才比较引用
    # 只会看到"同一个（已经被改过的）对象"，测不出这种回归；每轮结束后
    # 立刻序列化，才是真正对比"这一轮实际发出去的内容"。
    first_round_tools = json.dumps(provider.requests[0].tools, sort_keys=True)

    state2 = {
        "planner_messages": [
            {"role": "user", "content": "随便问点什么"},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"results": []}'},
        ],
        "tool_call_round": 1,
    }
    await run_planner_turn(
        state2, llm_registry=llm_registry, llm_provider_name="fake-llm", max_tool_call_rounds=3,
        tool_registry=tool_registry,
    )
    second_round_tools = json.dumps(provider.requests[1].tools, sort_keys=True)

    assert len(provider.requests) == 2
    assert first_round_tools == second_round_tools


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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
    )

    assert update == {"planner_gave_up": True}


async def test_run_planner_turn_gives_up_when_final_answer_attempt_raises():
    class _FirstScriptedThenRaisingProvider:
        def __init__(self, first_response):
            self._first_response = first_response
            self._call_count = 0

        async def complete(self, request):
            self._call_count += 1
            if self._call_count == 1:
                return self._first_response
            raise RuntimeError("boom")

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        _FirstScriptedThenRaisingProvider(
            ProviderResult(
                text="",
                tool_calls=[ToolCall(id="call_x", name="vector_search_tool", arguments="{}")],
            )
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
        tool_registry=_full_tool_registry(),
    )

    assert update == {"planner_gave_up": True}


async def test_run_planner_turn_gives_up_when_final_answer_attempt_leaks_malformed_tool_call_tokens():
    # 2026-08-27 真实案例：_run_final_answer_attempt 不传 tools 参数，文档
    # 假设这样"模型结构上不可能再请求工具调用"——对 DeepSeek 不成立，
    # 对话历史里出现过真实工具调用后，即使这次没声明 tools，模型仍然会
    # 把工具调用协议的专用特殊 token 当纯文本续写出来，原样出现在
    # result.text 里。命中这个标记必须当成总结失败处理，不能把这段
    # 内部协议 token 当成正常答案泄露给用户。
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
                ProviderResult(
                    text='让我尝试直接查询。\n<｜｜DSML｜｜tool_calls>'
                    '<｜｜DSML｜｜invoke name="structured_filter_query_tool">'
                    '<｜｜DSML｜｜parameter name="query_intent" string="true">'
                    "列出所有订单</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke>"
                    "</｜｜DSML｜｜tool_calls>",
                ),
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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
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

    registry = _fake_tool_registry(vector_search_tool=VECTOR_SEARCH_TOOL)
    context = _context(
        tenant_id="t1",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    update = await run_tool_calls(state, tool_registry=registry, context=context)

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
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm",
        ScriptedLLMProvider([ProviderResult(text='{"anchor": {"name": "网关超时示例"}}')]),
    )

    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"query_intent": "查网关超时示例是什么"}',
            }
        ],
    }

    graph_client = FakeGraphClientForStructuredQuery(rows=[{
        "standard_name": "示例错误码E502", "node_key": "示例错误码E502", "term_type": "error_code",
        "all_properties": {},
    }])
    registry = _fake_tool_registry(structured_filter_query_tool=STRUCTURED_FILTER_QUERY_TOOL)
    context = _context(
        tenant_id="t1",
        question="网关超时示例是什么",
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
    update = await run_tool_calls(state, tool_registry=registry, context=context)

    tool_message = update["planner_messages"][-1]
    assert "示例错误码E502" in tool_message["content"]
    assert graph_client.queried_tenant_ids == ["t1"]


async def test_run_tool_calls_reports_error_for_unknown_tool_name():
    """LLM 幻觉出一个不存在的工具名（tool_registry 里没有注册过）时，
    run_tool_calls 应该降级成 {"error": "未知工具: ..."} 观察结果正常
    返回，不应该抛 KeyError 崩溃——这是插件化改造之前 _dispatch_tool_call
    就有的既有行为，迁移后旧测试被删掉了，没有等价测试补上，这里补回来。"""
    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_1", "name": "does_not_exist_tool", "arguments": "{}"},
        ],
    }

    update = await run_tool_calls(state, tool_registry=_fake_tool_registry(), context=_context())

    tool_message = update["planner_messages"][-1]
    assert tool_message["role"] == "tool"
    parsed = json.loads(tool_message["content"])
    assert parsed == {"error": "未知工具: does_not_exist_tool"}


async def test_run_tool_calls_degrades_gracefully_when_structured_filter_query_resolution_fails():
    """独立参数生成调用返回非法 JSON 时，run_tool_calls 应该把这次工具调用
    降级成 {"error": ...} 观察结果正常返回，而不是让整个 run_tool_calls 崩溃。"""
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm",
        ScriptedLLMProvider([ProviderResult(text="这不是合法的 JSON")]),
    )

    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_1",
                "name": "structured_filter_query_tool",
                "arguments": '{"query_intent": "随便问点什么"}',
            }
        ],
    }

    registry = _fake_tool_registry(structured_filter_query_tool=STRUCTURED_FILTER_QUERY_TOOL)
    context = _context(
        tenant_id="t1",
        question="随便问点什么",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        terms=_TERMS,
        graph_client=FakeGraphClientForStructuredQuery(rows=[]),
        confirmed_relation_types=set(),
        term_type_schema={},
    )
    update = await run_tool_calls(state, tool_registry=registry, context=context)

    tool_message = update["planner_messages"][-1]
    assert tool_message["role"] == "tool"
    parsed = json.loads(tool_message["content"])
    assert "error" in parsed


async def test_run_tool_calls_reports_error_when_structured_filter_query_unconfigured():
    """graph_client 未配置（None）时，structured_filter_query_tool.execute()
    返回 {"error": "structured_filter_query_tool 未配置"}。

    这个检查现在发生在 Tool.execute() 内部（见
    app/agent/tools/structured_filter_query/tool.py）——Tool 协议里"是否
    配置好了"是每个工具自己的私事，通用的 run_tool_calls 分发层不应该
    认识某个具体工具的配置细节，那样会破坏插件化的初衷。这跟迁移前的
    行为有一处刻意的差异：迁移前 run_tool_calls 会在触发独立参数生成
    调用（resolve_arguments）之前就用工具专属的知识短路，省下一次注定
    会被拒绝的付费 LLM 调用；迁移后这个优化不再存在于通用分发层，
    resolve_arguments 总是会真的执行一次——实现者在报告里主动披露了
    这个代价，协调者审阅 Task 3 diff 时确认接受：只影响未配置 graph_client
    的场景（如 app/eval/runner.py 的评测脚本），不影响真实生产聊天流量，
    换来插件化架构的通用性（分发层不需要认识任何具体工具的配置细节）。"""
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider([ProviderResult(text='{"anchor": {"term_type": "订单号"}}')])
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)

    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_1",
                "name": "structured_filter_query_tool",
                "arguments": '{"query_intent": "随便问点什么"}',
            }
        ],
    }

    registry = _fake_tool_registry(structured_filter_query_tool=STRUCTURED_FILTER_QUERY_TOOL)
    context = _context(
        tenant_id="t1",
        question="随便问点什么",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        # graph_client 留默认 None——"未配置"
    )
    update = await run_tool_calls(state, tool_registry=registry, context=context)

    tool_message = update["planner_messages"][-1]
    parsed = json.loads(tool_message["content"])
    assert parsed == {"error": "structured_filter_query_tool 未配置"}
    assert len(provider.requests) == 1  # 独立参数生成调用确实发生了一次


async def test_run_tool_calls_annotates_expand_neighbors_with_association():
    """expand 返回的 neighbors 要按 hops 标注 association 文案——原
    graph_query_tool 分支的既有行为，迁移到 structured_filter_query_tool。"""
    _, vector_store, bm25_index = _build_store_and_index()
    embedding_registry = _embedding_registry()
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm",
        ScriptedLLMProvider([ProviderResult(text='{"anchor": {"name": "网关超时示例"}, "expand": {"hops": 2}}')]),
    )

    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {
                "id": "call_2",
                "name": "structured_filter_query_tool",
                "arguments": '{"query_intent": "网关超时示例关联什么，展开2跳"}',
            }
        ],
    }

    graph_client = FakeGraphClientForStructuredQuery(rows=[{
        "standard_name": "示例错误码E502", "node_key": "示例错误码E502", "term_type": "error_code",
        "all_properties": {},
        "neighbors": [{"related_name": "示例登录模块", "relation_type": "RELATED_TO", "hops": 2}],
    }])
    registry = _fake_tool_registry(structured_filter_query_tool=STRUCTURED_FILTER_QUERY_TOOL)
    context = _context(
        tenant_id="t1",
        question="网关超时示例关联什么",
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
    update = await run_tool_calls(state, tool_registry=registry, context=context)

    tool_message = update["planner_messages"][-1]
    parsed = json.loads(tool_message["content"])
    assert parsed["anchors"][0]["neighbors"][0]["association"] == "间接关联（经过 2 跳）"


def test_tool_schemas_no_longer_include_graph_query_tool():
    from pathlib import Path

    from app.agent.tool_registry import discover_tools

    tools_dir = Path(__file__).resolve().parents[2] / "app" / "agent" / "tools"
    registry = discover_tools(tools_dir)
    names = [s["function"]["name"] for s in registry.schemas()]
    assert "graph_query_tool" not in names
    assert "structured_filter_query_tool" in names


async def test_run_tool_calls_reports_error_for_malformed_arguments_without_crashing():
    registry = _fake_tool_registry(vector_search_tool=VECTOR_SEARCH_TOOL)
    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_3", "name": "vector_search_tool", "arguments": "不是合法JSON"}
        ],
    }

    update = await run_tool_calls(state, tool_registry=registry, context=_context(tenant_id="t1"))

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


async def test_run_tool_calls_executes_multiple_tools_concurrently():
    """同一轮请求了两个工具时，两次工具调用应该并发执行，不是排队顺序
    执行——用两个互等的 asyncio.Event 证明。"""
    started = {"call_1": asyncio.Event(), "call_2": asyncio.Event()}

    class _EventWaitingTool:
        async def resolve_arguments(self, raw_arguments, *, context):
            return raw_arguments

        async def execute(self, arguments, *, context):
            call_id = arguments["call_id"]
            started[call_id].set()
            other = "call_2" if call_id == "call_1" else "call_1"
            await asyncio.wait_for(started[other].wait(), timeout=5)
            return {"ok": call_id}, []

    # 同一个 fake Tool 实例注册在两个不同的工具名下——恢复原本要验证的
    # "同一轮混合请求多个不同名字的工具"场景，不需要真实工具实现里那些
    # 跟并发性无关的细节。
    tool = _EventWaitingTool()
    registry = _fake_tool_registry(
        vector_search_tool=tool, structured_filter_query_tool=tool,
    )

    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_1", "name": "vector_search_tool", "arguments": '{"call_id": "call_1"}'},
            {"id": "call_2", "name": "structured_filter_query_tool", "arguments": '{"call_id": "call_2"}'},
        ],
    }

    update = await run_tool_calls(state, tool_registry=registry, context=_context(tenant_id="t1"))

    contents_by_call_id = {r["tool_call_id"]: r["content"] for r in update["tool_results"]}
    assert contents_by_call_id["call_1"] == '{"ok": "call_1"}'
    assert contents_by_call_id["call_2"] == '{"ok": "call_2"}'
    # 顺序必须和 pending_tool_calls 原始顺序一致，不依赖谁先完成
    assert [r["tool_call_id"] for r in update["tool_results"]] == ["call_1", "call_2"]


async def test_run_tool_calls_downgrades_tool_execution_failure_to_error_observation_without_crashing():
    """当某个工具的 execute() 抛异常时，run_tool_calls() 把它降级成这次
    调用的 {"error": ...} 观察结果、正常返回，不让整个 run_tool_calls
    崩溃——同一轮里的另一个工具调用（call_1）应该照常成功完成，不受
    call_2 失败的影响。

    这是这次迁移里一处刻意的行为收紧，跟"未配置"场景（见上面
    test_run_tool_calls_reports_error_when_structured_filter_query_unconfigured
    的说明）同一类：迁移前只有 resolve_arguments 阶段的
    ToolArgumentResolutionError 会被这样降级，execute() 阶段抛出的异常会
    直接从 asyncio.gather(return_exceptions=True) 里重新抛出、让整条
    Planner 轮次崩溃；新架构把 resolve_arguments/execute 两个阶段统一包
    在同一个 try/except 里（见 app/agent/planner.py::run_tool_calls 的
    _execute_one），任何单个工具的失败都不应该拖垮同一轮里其它工具调用
    的结果，也不应该让整条 Agent 推理链路崩溃——实现者在报告里主动披露
    了这处行为收紧，协调者审阅 Task 3 diff 时确认接受：这跟本次会话贯穿
    始终的"优雅降级优先于崩溃"主题一致（Plan 1 的轮次耗尽兜底、独立参数
    生成调用失败时的 {"error": ...} 降级都是同一原则），但要求
    _execute_one 的这个 except 块必须记日志（exc_info=True）——降级只对
    用户/LLM 这一层负责，不能让真实的代码 bug 因为被吞成一句"error"消息
    就从开发者的可观测范围里消失。"""

    class _MaybeFailingTool:
        async def resolve_arguments(self, raw_arguments, *, context):
            return raw_arguments

        async def execute(self, arguments, *, context):
            if arguments.get("fail"):
                raise ValueError("模拟工具调用失败")
            return {"ok": "success"}, []

    tool = _MaybeFailingTool()
    registry = _fake_tool_registry(
        vector_search_tool=tool, structured_filter_query_tool=tool,
    )

    state = {
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_1", "name": "vector_search_tool", "arguments": '{}'},
            {"id": "call_2", "name": "structured_filter_query_tool", "arguments": '{"fail": true}'},
        ],
    }

    update = await run_tool_calls(state, tool_registry=registry, context=_context(tenant_id="t1"))

    contents_by_call_id = {r["tool_call_id"]: r["content"] for r in update["tool_results"]}
    assert contents_by_call_id["call_1"] == '{"ok": "success"}'
    parsed_call_2 = json.loads(contents_by_call_id["call_2"])
    assert "模拟工具调用失败" in parsed_call_2["error"]


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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
    )

    # 证明"最后陈述"重试真的发生过（第二轮脚本被消费、返回空文本导致放弃），
    # 而不是轮次耗尽时的旧短路逻辑直接放弃、根本没调用
    # _run_final_answer_attempt_streaming——旧代码下这个断言会因为
    # len(provider.requests) == 1 而失败。
    assert len(provider.requests) == 2
    assert update == {"planner_gave_up": True}
    assert tool_status_calls == 0


async def test_run_planner_turn_streaming_gives_up_when_final_answer_attempt_leaks_malformed_tool_call_tokens():
    # 流式版本的同一个真实案例（见 test_run_planner_turn_gives_up_when_
    # final_answer_attempt_leaks_malformed_tool_call_tokens 的非流式版本）：
    # 命中标记的这一句要被替换成安全兜底文案再推给用户（不能让内部协议
    # token 原样出现在流式增量里），整轮总结按失败处理，不落入
    # planner_messages/answer_text。
    llm_registry = ProviderRegistry()
    provider = ScriptedStreamingLLMProvider(
        [
            [
                ProviderStreamChunk(
                    tool_calls=[ToolCall(id="call_1", name="vector_search_tool", arguments="{}")]
                )
            ],
            [
                ProviderStreamChunk(
                    text='让我尝试直接查询。\n<｜｜DSML｜｜tool_calls>'
                    '<｜｜DSML｜｜invoke name="structured_filter_query_tool">'
                    '<｜｜DSML｜｜parameter name="query_intent" string="true">'
                    "列出所有订单</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke>"
                    "</｜｜DSML｜｜tool_calls>",
                )
            ],
        ]
    )
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
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
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
        tool_registry=_full_tool_registry(),
    )

    assert update["planner_gave_up"] is True
    assert "answer_text" not in update
    assert all("DSML" not in chunk for chunk in sent_chunks)
    assert LITE_SAFETY_FALLBACK_SENTENCE in sent_chunks


async def test_run_planner_turn_streaming_gives_up_when_final_answer_attempt_raises():
    class _RaisingStreamingProvider:
        def __init__(self, first_round):
            self._first_round = first_round
            self._call_count = 0

        async def complete(self, request):
            raise NotImplementedError("此 Fake 只用于测试流式路径")

        async def stream_complete_with_tools(self, request):
            self._call_count += 1
            if self._call_count == 1:
                for chunk in self._first_round:
                    yield chunk
                return
            raise RuntimeError("boom")
            yield  # pragma: no cover - unreachable, makes this a generator

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        _RaisingStreamingProvider(
            [ProviderStreamChunk(tool_calls=[ToolCall(id="call_1", name="vector_search_tool", arguments="{}")])]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
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
        tool_registry=_full_tool_registry(),
    )

    assert update["planner_gave_up"] is True
    assert update["streamed_round_texts"] == []


async def test_run_planner_turn_streaming_final_answer_attempt_preserves_partial_text_on_failure():
    """回归测试 Finding 1 的修复：最后陈述流式调用中途失败时，已经推送
    给用户的部分文本必须并入 streamed_round_texts，供 output_safety_node
    的完整规则+泄露审查覆盖，而不是随着 except 分支被直接丢弃。"""

    class _PartialThenRaisingProvider:
        def __init__(self, first_round):
            self._first_round = first_round
            self._call_count = 0

        async def complete(self, request):
            raise NotImplementedError("此 Fake 只用于测试流式路径")

        async def stream_complete_with_tools(self, request):
            self._call_count += 1
            if self._call_count == 1:
                for chunk in self._first_round:
                    yield chunk
                return
            yield ProviderStreamChunk(text="已经查到部分信息。")
            raise RuntimeError("boom")

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        _PartialThenRaisingProvider(
            [ProviderStreamChunk(tool_calls=[ToolCall(id="call_1", name="vector_search_tool", arguments="{}")])]
        ),
    )
    state = {
        "planner_messages": [{"role": "user", "content": "问题"}],
        "tool_call_round": 3,
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
        banned_terms=None,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
        tool_registry=_full_tool_registry(),
    )

    assert update["planner_gave_up"] is True
    assert "已经查到部分信息。" in update["streamed_round_texts"]
    assert sent_chunks == ["已经查到部分信息。"]


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
        tool_registry=_full_tool_registry(),
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
        tool_registry=_full_tool_registry(),
    )

    assert provider.requests[1].tools is None
