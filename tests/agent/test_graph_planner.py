from pathlib import Path

from app.agent.graph import _build_planner_system_prompt, build_agent_graph
from app.agent.tool_registry import discover_tools
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

# 这些测试都走 enable_autonomous_planning=True 的 Planner 路径，真的会
# 用到 tool_registry 里注册的工具（vector_search_tool/
# structured_filter_query_tool），用 discover_tools 扫描真实的
# app/agent/tools/ 目录构建一次，供所有调用点复用。
_TOOL_REGISTRY = discover_tools(Path(__file__).resolve().parents[2] / "app" / "agent" / "tools")


def test_build_planner_system_prompt_matches_content_exactly():
    """精确逐字符比对（不是"拼接公式自洽"这种同义反复的检查）——这是这次
    插件化改造touch到的、面向 LLM 的最高杠杆字符串，之前完全没有测试
    覆盖：_PLANNER_BASE_PROMPT 打错字、manifest.yaml 的 trigger_cue 字段
    丢失、trigger_cues() 里一个 .get() 拼写错误，都会在这里被拦下，不会
    悄悄带着错误内容上线。硬编码的这段期望值不能改成引用
    _PLANNER_BASE_PROMPT/_TOOL_REGISTRY.trigger_cues() 本身——那样测试
    的就是"函数输出等于它自己用到的常量"，是重言式，测不出常量本身
    出错的情况。"""
    prompt = _build_planner_system_prompt(_TOOL_REGISTRY)

    assert prompt == (
        "你是客服问答助手。可以调用 vector_search_tool 检索知识库、"
        "structured_filter_query_tool 查询知识图谱里的实体数量/满足条件的实体列表。"
        "有足够信息时直接给出最终答案，不要编造资料中没有的内容；"
        "信息不足以回答时也不要编造。"
        "看到「多少个」「数量」等计数意图时，应该用 structured_filter_query_tool 给出确定数字，"
        "不能仅凭检索到的文档片段猜测，也不能因为一次调用没查到就直接放弃。"
    )


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


def _dependencies(tenant_id: str = "t1"):
    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时请先重启路由器。",
            tenant_id=tenant_id,
            metadata={},
        )
    ]
    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    return records, vector_store, bm25_index, embedding_registry


async def _seed(records, vector_store, bm25_index) -> None:
    await vector_store.upsert(records)
    bm25_index.index(records)


async def test_planner_calls_tool_once_then_answers():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

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
                ),
                ProviderResult(text="重启路由器即可解决。"),
            ]
        ),
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
        enable_autonomous_planning=True,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "tenant_id": "t1"}
    )

    assert result["final_text"] == "重启路由器即可解决。"
    assert result["used_sources"] == ["faq/network.md"]
    assert result["fallback_triggered"] is False
    assert result.get("ticket_id") is None


async def test_planner_exceeding_max_rounds_falls_back_and_creates_ticket():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

    # 每一轮都要求调用工具；轮次耗尽后还有一次"最后陈述"尝试，这里让它
    # 也返回空文本，验证两层都失败时仍然走静态兜底+创建工单。
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(id=f"call_{i}", name="vector_search_tool", arguments='{"query": "x"}')
                    ],
                )
                for i in range(1, 3)
            ]
            + [ProviderResult(text="")]
        ),
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
        enable_autonomous_planning=True,
        max_tool_call_rounds=1,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "tenant_id": "t1"},
        config={"recursion_limit": 50},
    )

    assert result["fallback_triggered"] is True
    assert result["ticket_id"]
    assert "转" in result["final_text"] or "人工" in result["final_text"]


async def test_planner_final_answer_attempt_succeeds_avoids_ticket():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                ProviderResult(
                    text="",
                    tool_calls=[
                        ToolCall(id=f"call_{i}", name="vector_search_tool", arguments='{"query": "x"}')
                    ],
                )
                for i in range(1, 3)
            ]
            + [ProviderResult(text="根据已经查到的信息，建议重启路由器。")]
        ),
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
        enable_autonomous_planning=True,
        max_tool_call_rounds=1,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "tenant_id": "t1"},
        config={"recursion_limit": 50},
    )

    assert result["fallback_triggered"] is False
    assert result.get("ticket_id") is None
    assert result["final_text"] == "根据已经查到的信息，建议重启路由器。"


async def test_planner_does_not_surface_another_tenants_records():
    records, vector_store, bm25_index, embedding_registry = _dependencies(tenant_id="t1")
    await _seed(records, vector_store, bm25_index)

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
                            arguments='{"query": "网络连不上怎么办", "tenant_id": "t1"}',
                        )
                    ],
                ),
                ProviderResult(text="没有找到相关资料。"),
            ]
        ),
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
        enable_autonomous_planning=True,
    )

    result = await graph.ainvoke(
        {"question": "网络连不上怎么办？", "tenant_id": "t2"}
    )

    assert result["used_sources"] == []


async def test_planner_graph_uses_structured_filter_query_tool_with_term_guard_context():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

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
                            name="structured_filter_query_tool",
                            arguments='{"query_intent": "查网关超时示例的标准名称"}',
                        )
                    ],
                ),
                ProviderResult(text='{"anchor": {"name": "网关超时示例"}}'),  # 独立参数生成调用
                ProviderResult(text="已确认标准名称是示例错误码E502。"),
            ]
        ),
    )
    terms = [
        Term(
            tenant_id="t1",
            node_key="示例错误码E502",
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
        )
    ]

    class FakeGraphClient:
        async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
            return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]

        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            return {
                "rows": [{
                    "standard_name": "示例错误码E502",
                    "node_key": "示例错误码E502",
                    "term_type": "error_code",
                    "all_properties": {},
                }],
                "total_count": 1,
            }

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tool_registry=_TOOL_REGISTRY,
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
        terms=terms,
        graph_client=FakeGraphClient(),
        confirmed_relation_types=set(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    result = await graph.ainvoke(
        {"question": "网关超时示例是什么", "tenant_id": "t1"}
    )

    assert result["final_text"] == "已确认标准名称是示例错误码E502。"


class ScriptedStreamingLLMProvider:
    """跟 test_planner.py 里同名类是同一个模式：按调用顺序消费预先编排好
    的"每轮一组 chunk"脚本。这里额外记录每次是走 complete() 还是
    stream_complete_with_tools()，用来断言"不支持流式的 provider 走
    complete()"这条回退路径。"""

    def __init__(self, rounds: list[list[ProviderStreamChunk]]) -> None:
        self._rounds = list(rounds)
        self.stream_call_count = 0

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise NotImplementedError("此 Fake 只用于测试流式路径")

    async def stream_complete_with_tools(self, request: ProviderRequest):
        self.stream_call_count += 1
        for chunk in self._rounds.pop(0):
            yield chunk


async def test_planner_streams_final_answer_and_emits_tool_status():
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

    llm_registry = ProviderRegistry()
    provider = ScriptedStreamingLLMProvider(
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
            ],
            [ProviderStreamChunk(text="重启"), ProviderStreamChunk(text="路由器即可解决。")],
        ]
    )
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)

    received_chunks: list[str] = []
    tool_status_count = 0

    async def on_answer_chunk(text: str) -> None:
        received_chunks.append(text)

    async def on_tool_status() -> None:
        nonlocal tool_status_count
        tool_status_count += 1

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tool_registry=_TOOL_REGISTRY,
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    result = await graph.ainvoke({"question": "网络连不上怎么办？", "tenant_id": "t1"})

    assert result["final_text"] == "重启路由器即可解决。"
    assert received_chunks == ["重启路由器即可解决。"]
    assert tool_status_count == 1
    assert provider.stream_call_count == 2


class StreamingProviderWithSemanticReviewCapture:
    """跟 ScriptedStreamingLLMProvider 同一个流式脚本模式，区别是
    complete() 不再 raise NotImplementedError——output_safety_node 的
    semantic_safety_review 对同一个 llm_provider_name 发起非流式
    complete() 调用，这里改成捕获每次 complete() 收到的 request 并返回
    固定的"安全"JSON 结果，让审查流程能正常走完，同时供测试断言语义
    审查到底看到了哪些文本。"""

    def __init__(self, rounds: list[list[ProviderStreamChunk]]) -> None:
        self._rounds = list(rounds)
        self.stream_call_count = 0
        self.complete_requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.complete_requests.append(request)
        return ProviderResult(text='{"is_safe": true, "reason": ""}')

    async def stream_complete_with_tools(self, request: ProviderRequest):
        self.stream_call_count += 1
        for chunk in self._rounds.pop(0):
            yield chunk


async def test_output_safety_reviews_leading_commentary_text_from_earlier_planner_round():
    """Finding 1 回归测试：第 1 轮"让我查一下。"这句前置说明文字已经
    通过 on_answer_chunk 实时推送给用户展示过，但它从未进入最后一轮的
    answer_text。output_safety_node 的完整安全审查（这里用
    semantic_safety_review 实际收到的 request.messages 代表"完整审查"）
    必须真的看到这段文字，而不是只审查最后一轮的 answer_text。"""
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

    llm_registry = ProviderRegistry()
    provider = StreamingProviderWithSemanticReviewCapture(
        [
            [
                ProviderStreamChunk(text="让我查一下。"),
                ProviderStreamChunk(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="vector_search_tool",
                            arguments='{"query": "网络连不上怎么办"}',
                        )
                    ]
                ),
            ],
            [ProviderStreamChunk(text="重启路由器即可解决。")],
        ]
    )
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)

    async def on_answer_chunk(text: str) -> None:
        pass

    async def on_tool_status() -> None:
        pass

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tool_registry=_TOOL_REGISTRY,
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    result = await graph.ainvoke({"question": "网络连不上怎么办？", "tenant_id": "t1"})

    assert result["final_text"] == "重启路由器即可解决。"
    # 语义安全审查（走 complete()）必须收到过第一轮那句从未进入
    # answer_text 的前置说明文字，不能只审查最后一轮的答案。
    reviewed_texts = " ".join(
        message.get("content") or ""
        for request in provider.complete_requests
        for message in request.messages
    )
    assert "让我查一下。" in reviewed_texts


async def test_planner_falls_back_to_non_streaming_when_provider_lacks_tool_streaming():
    """provider 只有 complete()、没有 stream_complete_with_tools 时，即使
    传了 on_answer_chunk，也必须透明回退到现有的一次性 run_planner_turn()
    ——这是 Global Constraints 里"不支持就用旧行为"这条硬约束的回归测试。
    """
    records, vector_store, bm25_index, embedding_registry = _dependencies()
    await _seed(records, vector_store, bm25_index)

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
                ),
                ProviderResult(text="重启路由器即可解决。"),
            ]
        ),
    )

    received_chunks: list[str] = []

    async def on_answer_chunk(text: str) -> None:
        received_chunks.append(text)

    async def on_tool_status() -> None:
        pass

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tool_registry=_TOOL_REGISTRY,
        query_rewrite_enabled=False,
        enable_autonomous_planning=True,
        on_answer_chunk=on_answer_chunk,
        on_tool_status=on_tool_status,
    )

    result = await graph.ainvoke({"question": "网络连不上怎么办？", "tenant_id": "t1"})

    assert result["final_text"] == "重启路由器即可解决。"
    # ScriptedLLMProvider（非流式 Fake）没有 stream_complete_with_tools，
    # 走的是 complete()——on_answer_chunk 完全没被调用，行为跟这次改动
    # 之前一模一样。
    assert received_chunks == []
