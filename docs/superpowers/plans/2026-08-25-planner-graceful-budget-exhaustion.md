# Planner 轮次耗尽兜底体验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当 Planner 工具调用轮次预算耗尽、且 LLM 仍要求调用工具时，不再直接放弃并吐出写死的"转人工"文案，而是先尝试用已有信息给出一次总结性回答；前端不再把每轮工具调用前的叙述文字永久丢弃，改成默认折叠的"推理过程"区域。

**Architecture:** 后端在 `app/agent/planner.py` 的 `run_planner_turn`/`run_planner_turn_streaming` 里，把"轮次耗尽 → 直接放弃"改成"轮次耗尽 → 再发起一次不带 `tools` 参数的 LLM 调用，要求基于已有信息总结 → 成功就当正常完成一轮处理（走完整安全审查），失败才真正放弃（退回今天的行为）"。前端在 `useAgentChat.ts` 的 `tool_status` 处理器里，把即将被清空的文字先存进新的 `reasoningTrail` 数组，`MessageBubble.tsx` 渲染一个默认折叠的展开区块展示它。

**Tech Stack:** Python 3.12 / FastAPI 后端（`app/agent/`），TypeScript / React 前端（`frontend/src/`），pytest，无前端测试框架（用 `tsc --noEmit` + 手动浏览器验证）。

**Spec:** `docs/superpowers/specs/2026-08-25-planner-graceful-budget-exhaustion-design.md`

## Global Constraints

- 后端改动只涉及 `app/agent/planner.py`；不改 `app/agent/graph.py` 的图结构/路由、不改 `app/agent/tools.py`、不改查询引擎（`app/graphrag/` 下任何文件）。
- "最后陈述"调用失败时的行为必须与今天完全一致（`planner_gave_up: True` → 静态文案 + 人工工单）。
- "最后陈述"产出的文本必须经过 `output_safety_node` 的完整规则+语义审查（即返回值里 `fallback_triggered` 键不能出现/为 `False`），不允许绕过。
- 前端改动只涉及 `frontend/src/hooks/useAgentChat.ts` 和 `frontend/src/components/MessageBubble.tsx`；不改变 `message.text`/`final` 事件的既有渲染逻辑。
- 不新增配置开关，不调整 `max_tool_call_rounds` 默认值（`app/config/settings.py:148`，保持 `3`）。

---

### Task 1: 非流式路径的"最后陈述"兜底（`run_planner_turn`）

**Files:**
- Modify: `app/agent/planner.py:28-100`（`_build_tool_call_round_result`、`run_planner_turn`，新增 `_FINAL_ANSWER_INSTRUCTION`、`_run_final_answer_attempt`）
- Test: `tests/agent/test_planner.py:139-168`（更新已有测试）、新增测试

**Interfaces:**
- Consumes：`ProviderRegistry.run(capability, request, *, provider_name) -> ProviderResult`（已有，`app/providers/registry.py`）；`ProviderRequest(messages=..., tools=..., tool_choice=...)`（已有，`app/providers/base.py`，`tools`/`tool_choice` 都是可选字段，不传即为 `None`）。
- Produces：`_build_tool_call_round_result(messages, answer_text, tool_calls) -> dict`（签名变化：去掉 `round_num`/`max_tool_call_rounds` 两个参数——轮次是否耗尽现在由调用方在调用前判断，这个函数只管"没耗尽时怎么构造返回值"）；`_run_final_answer_attempt(messages, *, llm_registry, llm_provider_name) -> dict`（新函数，Task 2 的流式版本会参考同一份 `_FINAL_ANSWER_INSTRUCTION` 常量）。

- [ ] **Step 1: 读一遍当前实现，确认起点**

打开 `app/agent/planner.py`，确认第 28-100 行就是下面要改的 `_build_tool_call_round_result`/`run_planner_turn`，没有被其他改动动过。

- [ ] **Step 2: 写失败的测试——轮次耗尽但"最后陈述"调用也失败时仍然放弃**

在 `tests/agent/test_planner.py` 里，把已有的 `test_run_planner_turn_gives_up_when_max_rounds_exceeded`（第 139-168 行）整体替换成：

```python
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
```

同时在这个测试后面新增两个测试：

```python
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
```

- [ ] **Step 2b: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -k "final_answer_attempt or gives_up_when" -v`
Expected: 三个新/改的测试都 FAIL（`test_run_planner_turn_gives_up_when_final_answer_attempt_also_returns_empty_text` 会因为 `ScriptedLLMProvider._responses` 只被 `.pop(0)` 一次、第二次调用不会发生而跟今天的旧实现behavior不一致——今天的实现在轮次耗尽时根本不会发起第二次调用，所以这个新测试传入的两个 `ProviderResult` 里第二个永远不会被消费，`update` 会是 `{"planner_gave_up": True}`，看起来"意外地"PASS——这正常，因为这一步还没改实现，先往下走）。

**这一步的真正验证点是接下来两个新增测试必须 FAIL**——`test_run_planner_turn_final_answer_attempt_succeeds_when_rounds_exhausted` 断言 `planner_gave_up is False`，但今天的实现在轮次耗尽时无条件返回 `{"planner_gave_up": True}`，所以会失败；`test_run_planner_turn_final_answer_attempt_does_not_pass_tools` 断言 `provider.requests[1].tools is None`，但今天的实现根本不会有第二次请求，`provider.requests` 长度是 1，取 `[1]` 会 `IndexError`。确认这两个测试确实以预期方式失败后继续。

- [ ] **Step 3: 实现——新增 `_FINAL_ANSWER_INSTRUCTION` 常量和 `_run_final_answer_attempt` 函数，改 `_build_tool_call_round_result`/`run_planner_turn`**

把 `app/agent/planner.py` 第 25-100 行（从 `_TOOL_SCHEMAS = ...` 到 `run_planner_turn` 结尾）整体替换成：

```python
_TOOL_SCHEMAS = [VECTOR_SEARCH_TOOL_SCHEMA, STRUCTURED_FILTER_QUERY_TOOL_SCHEMA]

_FINAL_ANSWER_INSTRUCTION = (
    "你已经达到本轮对话可用的工具调用次数上限，不能再调用任何工具了。"
    "请基于你目前已经查询到的全部信息，尽力给用户一个有帮助的回答：如果已经有"
    "明确的结论或数字，直接给出；如果现有信息不足以给出确定结论，清楚说明你"
    "目前掌握的情况、以及为什么无法进一步确认（比如某个维度在当前数据里没有"
    "区分度、或者查询本身没有找到匹配结果），不要用套话搪塞，也绝不能编造"
    "没有查到的数据。"
)


def _build_tool_call_round_result(
    messages: list[dict[str, Any]],
    answer_text: str,
    tool_calls: list[ToolCall],
) -> dict[str, Any]:
    """构造"这一轮模型请求了工具调用、且轮次预算还没耗尽"场景下的返回值：
    把 assistant 消息（带 tool_calls 字段）追加进对话历史，返回待执行的
    工具调用列表。run_planner_turn（非流式）和 run_planner_turn_streaming
    （流式）在这一步的逻辑完全一样，抽成这个共用函数，避免两处重复维护。

    轮次是否耗尽由调用方在调用这个函数之前就判断好——耗尽时调用方会转去
    调 _run_final_answer_attempt（或它的流式版本），根本不会走到这个
    函数，所以这个函数不再需要知道 round_num/max_tool_call_rounds。
    """
    messages = [
        *messages,
        {
            "role": "assistant",
            "content": answer_text or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ],
        },
    ]
    return {
        "planner_messages": messages,
        "pending_tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
        ],
    }


async def _run_final_answer_attempt(
    messages: list[dict[str, Any]],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
) -> dict[str, Any]:
    """轮次预算耗尽、且这一轮 LLM 仍要求调用工具时的兜底：不带 tools 参数
    再调用一次 LLM，要求它基于已有信息给出最后的总结性回答，而不是直接
    放弃（今天的行为）。

    messages 是这一轮开始前的历史（不包含这次被拒绝的 tool_calls 请求，
    跟轮次未耗尽时"不能把申请了工具调用但没执行的 assistant 消息留在
    历史里"这条原则一致）。

    成功（拿到非空文本）：按跟"LLM 主动决定不再调工具、直接给出最终答案"
    完全一样的返回形状处理——调用方（route_after_planner）会把这当成
    正常完成一轮处理，流转到 planner_responder_node -> output_safety_node
    做完整的规则+语义安全审查，不会创建人工工单。

    失败（调用异常或返回空文本）：退回 {"planner_gave_up": True}，走今天
    完全一样的路径（fallback_node 静态文案 + create_ticket_node 创建
    工单）——这是这个函数"下限不比今天差"的保证。
    """
    final_messages = [*messages, {"role": "system", "content": _FINAL_ANSWER_INSTRUCTION}]
    try:
        result = await llm_registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=final_messages),
            provider_name=llm_provider_name,
        )
    except Exception:
        return {"planner_gave_up": True}
    if not result.text:
        return {"planner_gave_up": True}
    messages = [*messages, {"role": "assistant", "content": result.text}]
    return {
        "planner_messages": messages,
        "answer_text": result.text,
        "planner_gave_up": False,
    }


async def run_planner_turn(
    state: dict[str, Any],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    max_tool_call_rounds: int,
) -> dict[str, Any]:
    """执行一轮 Planner 推理：调用 LLM，决定"再调工具"还是"给出最终答案"。

    round_num 语义是"已经完成的工具调用轮次"；只有当 LLM 在 round_num 已经
    达到上限时仍要求调用工具，才会转去 _run_final_answer_attempt 做最后
    一次总结性回答的尝试（成功就当正常完成，失败才真正放弃）——绝不在
    轮次耗尽后仍然执行它请求的工具，那样等于绕过了轮次上限。
    """
    messages = list(state.get("planner_messages", []))
    round_num = state.get("tool_call_round", 0)

    result = await llm_registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=messages, tools=_TOOL_SCHEMAS, tool_choice="auto"),
        provider_name=llm_provider_name,
    )

    if result.tool_calls:
        if round_num >= max_tool_call_rounds:
            return await _run_final_answer_attempt(
                messages, llm_registry=llm_registry, llm_provider_name=llm_provider_name,
            )
        return _build_tool_call_round_result(messages, result.text, result.tool_calls)

    messages.append({"role": "assistant", "content": result.text})
    return {
        "planner_messages": messages,
        "answer_text": result.text,
        "planner_gave_up": False,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -k "final_answer_attempt or gives_up_when or returns_pending_tool_calls or returns_answer_when" -v`
Expected: 全部 PASS（含没改过的 `test_run_planner_turn_returns_pending_tool_calls_when_llm_requests_a_tool`/`test_run_planner_turn_returns_answer_when_llm_stops_calling_tools` 两个既有测试，确认没有破坏轮次未耗尽时的行为）。

- [ ] **Step 5: 跑这个文件的全部测试，确认没有连带破坏其它用例**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -v`
Expected: 全部 PASS（这个文件里还有 `run_tool_calls`/`_dispatch_tool_call`/`route_after_planner` 相关的测试，这一步的改动不涉及它们，应该不受影响）。

- [ ] **Step 5b: 修一个会被这次改动破坏的图级别集成测试**

`tests/agent/test_graph_planner.py` 里的 `test_planner_exceeding_max_rounds_falls_back_and_creates_ticket`（第 100-141 行）用一个只有 2 个响应（都请求工具调用）的 `ScriptedLLMProvider` 验证轮次耗尽后的行为——按这次改动，第 2 轮判定耗尽后会多发起一次 `_run_final_answer_attempt` 调用，但这个 fake provider 已经没有更多响应可弹，会在这一步之前就 `IndexError`，而不是命中这个测试原本想测的"耗尽后转人工"路径。

把这个测试整体替换成：

```python
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
```

紧接着在它后面新增一个测试，覆盖"最后陈述成功、不创建工单"这个新行为（3 个响应：2 轮工具调用请求 + 1 次成功的总结性回答；不额外加第 4 个响应给 `output_safety_node` 的语义审查——`test_planner_calls_tool_once_then_answers` 这个既有测试同样只给够"轮次+最终答案"这些响应、没有单独为语义审查配响应，也一直正常通过，因为 `semantic_safety_review` 内部对 LLM 调用异常整体 `except Exception` 兜底成"放行但标记未审查"，这里延续同一个约定，不引入不一致的写法）：

```python
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
```

- [ ] **Step 5c: 运行 `test_graph_planner.py` 确认这两个测试符合预期**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_planner.py -v`
Expected: 全部 PASS（含这一步改动的两个测试，以及文件里其它没动过的测试——`test_planner_calls_tool_once_then_answers`/`test_planner_does_not_surface_another_tenants_records`/`test_planner_graph_uses_structured_filter_query_tool_with_term_guard_context`/`test_planner_streams_final_answer_and_emits_tool_status`/`test_output_safety_reviews_leading_commentary_text_from_earlier_planner_round`/`test_planner_falls_back_to_non_streaming_when_provider_lacks_tool_streaming`）。

- [ ] **Step 6: Commit**

```bash
git add app/agent/planner.py tests/agent/test_planner.py tests/agent/test_graph_planner.py
git commit -m "feat(agent): retry with a final summarizing call before giving up on round exhaustion"
```

---

### Task 2: 流式路径的"最后陈述"兜底（`run_planner_turn_streaming`）

**Files:**
- Modify: `app/agent/planner.py:125-208`（`run_planner_turn_streaming`，新增 `_run_final_answer_attempt_streaming`）
- Test: `tests/agent/test_planner.py:727-768`（更新已有测试）、新增测试

**Interfaces:**
- Consumes：Task 1 产出的 `_FINAL_ANSWER_INSTRUCTION`（复用同一份指令文案，不要在这个文件里重复定义一份内容相近但字面不同的字符串）；`ProviderRegistry.stream_with_tools(capability, request, *, provider_name) -> AsyncIterator[ProviderStreamChunk]`（已有）；`_split_stream_text_and_tool_calls`/`stream_sentences`/`check_text`（已有，`run_planner_turn_streaming` 主循环已经在用的同一套）。
- Produces：`_run_final_answer_attempt_streaming(messages, *, llm_registry, llm_provider_name, banned_terms, on_answer_chunk, streamed_round_texts) -> dict`（新函数，只被 `run_planner_turn_streaming` 调用）。

- [ ] **Step 1: 写失败的测试——流式路径轮次耗尽但"最后陈述"调用也失败**

在 `tests/agent/test_planner.py` 里，先确认文件顶部已经 import 了 `ScriptedStreamingLLMProvider`（Task 1 结束后这个类应该还在文件里，位置大约在第 480-511 行附近，不用改）。

把已有的 `test_run_planner_turn_streaming_gives_up_without_tool_status_when_rounds_exhausted`（第 727-768 行）整体替换成：

```python
async def test_run_planner_turn_streaming_gives_up_when_final_answer_attempt_also_fails():
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
                ],
                [ProviderStreamChunk(text="")],
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
```

同时在这个测试后面新增两个测试：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -k "streaming_final_answer_attempt or streaming_gives_up" -v`
Expected: `test_run_planner_turn_streaming_final_answer_attempt_succeeds_when_rounds_exhausted` FAIL（今天的实现轮次耗尽时无条件 `{"planner_gave_up": True}`，`update["planner_gave_up"]` 不是 `False`）；`test_run_planner_turn_streaming_final_answer_attempt_does_not_pass_tools` FAIL（`provider.requests` 长度是 1，`[1]` 越界）。

- [ ] **Step 3: 实现——新增 `_run_final_answer_attempt_streaming`，改 `run_planner_turn_streaming` 尾部**

在 `app/agent/planner.py` 里，`_split_stream_text_and_tool_calls` 函数（第 103-122 行）和 `run_planner_turn_streaming` 函数（第 125-208 行）之间，插入新函数：

```python
async def _run_final_answer_attempt_streaming(
    messages: list[dict[str, Any]],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    banned_terms: list[str] | None,
    on_answer_chunk: Callable[[str], Awaitable[None]],
    streamed_round_texts: list[str],
) -> dict[str, Any]:
    """run_planner_turn_streaming 版本的轮次耗尽兜底：跟 _run_final_answer_
    attempt（非流式版本）语义一致，区别是这次调用同样走 stream_with_tools()
    （不传 tools，模型结构上不可能再请求工具调用）边生成边推送，跟主循环
    共用同一套逐句 check_text 安全替换逻辑，让用户看到的体验是从"查询
    过程"无缝过渡到"总结陈述"，而不是先看到一段查询叙述、中间断一下、
    再冒出一句不相关的静态兜底文案。

    不调用 on_tool_status()——这次不是"还在查"，是"在总结"，延续
    run_planner_turn_streaming 里同一条原则（见该函数文档字符串）。

    streamed_round_texts 是这一轮开始前已经流式展示过的所有轮次文本
    （含这一轮被拒绝前那句"让我查一下xxx"式的叙述，即使它没有被持久化
    进 planner_messages）——成功时把这次的总结文本也并进去，交给
    output_safety_node 做完整安全审查，跟正常轮次的处理方式完全一致。
    """
    final_messages = [*messages, {"role": "system", "content": _FINAL_ANSWER_INSTRUCTION}]
    try:
        raw_stream = llm_registry.stream_with_tools(
            ProviderCapability.LLM,
            ProviderRequest(messages=final_messages),
            provider_name=llm_provider_name,
        )
        tool_calls_box: list[list[ToolCall] | None] = [None]
        raw_text_parts: list[str] = []
        text_stream = _split_stream_text_and_tool_calls(raw_stream, tool_calls_box, raw_text_parts)

        sent_sentences: list[str] = []
        any_sentence_substituted = False
        async for sentence in stream_sentences(text_stream):
            safety_result = check_text(sentence, banned_terms=banned_terms, include_email=False)
            if safety_result.is_safe:
                safe_sentence = sentence
            else:
                safe_sentence = LITE_SAFETY_FALLBACK_SENTENCE
                any_sentence_substituted = True
            await on_answer_chunk(safe_sentence)
            sent_sentences.append(safe_sentence)
    except Exception:
        return {"planner_gave_up": True}

    full_text = "".join(sent_sentences) if any_sentence_substituted else "".join(raw_text_parts)
    if not full_text:
        return {"planner_gave_up": True}

    messages = [*messages, {"role": "assistant", "content": full_text}]
    return {
        "planner_messages": messages,
        "answer_text": full_text,
        "planner_gave_up": False,
        "streamed_round_texts": [*streamed_round_texts, full_text],
    }
```

然后把 `run_planner_turn_streaming` 函数体最后一段（第 191-208 行，`tool_calls = tool_calls_box[0]` 开始到函数结尾）替换成：

```python
    tool_calls = tool_calls_box[0]
    if tool_calls:
        if round_num >= max_tool_call_rounds:
            return await _run_final_answer_attempt_streaming(
                messages,
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                banned_terms=banned_terms,
                on_answer_chunk=on_answer_chunk,
                streamed_round_texts=streamed_round_texts,
            )
        result = _build_tool_call_round_result(messages, full_text, tool_calls)
        await on_tool_status()
        result["streamed_round_texts"] = streamed_round_texts
        return result

    messages = [*messages, {"role": "assistant", "content": full_text}]
    return {
        "planner_messages": messages,
        "answer_text": full_text,
        "planner_gave_up": False,
        "streamed_round_texts": streamed_round_texts,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -k "streaming" -v`
Expected: 全部 PASS（含既有的 `test_run_planner_turn_streaming_forwards_text_deltas_for_direct_answer`/`test_run_planner_turn_streaming_does_not_forward_text_for_pure_tool_call_round`/`test_run_planner_turn_streaming_replaces_sentence_matching_banned_term`/`test_run_planner_turn_streaming_preserves_embedded_newline_when_no_substitution`/`test_run_planner_turn_streaming_uses_joined_sentences_not_raw_text_when_substituted`，确认没有破坏轮次未耗尽时的流式行为）。

- [ ] **Step 5: 跑这个文件的全部测试**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_planner.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add app/agent/planner.py tests/agent/test_planner.py
git commit -m "feat(agent): retry with a final summarizing call on round exhaustion in the streaming path"
```

---

### Task 3: 前端保留每轮工具调用前的叙述文字（推理过程）

**Files:**
- Modify: `frontend/src/hooks/useAgentChat.ts`（`ChatMessage` 接口、三处 `ChatMessage` 对象构造、`tool_status` 事件处理器）
- Modify: `frontend/src/components/MessageBubble.tsx`（新增 `ReasoningTrail` 组件，接入 `MessageBubble`）

**Interfaces:**
- Consumes：无新的后端接口依赖——`tool_status`/`delta`/`final` 事件形状不变，这一步纯前端。
- Produces：`ChatMessage.reasoningTrail: string[]`（新字段），`MessageBubble` 内部新增的 `ReasoningTrail` 组件（不导出，只在这个文件内使用）。

- [ ] **Step 1: 改 `ChatMessage` 接口，新增 `reasoningTrail` 字段**

在 `frontend/src/hooks/useAgentChat.ts` 里，把：

```typescript
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  usedSources: string[]
  isStreaming: boolean
  isError?: boolean
  statusText?: string
}
```

改成：

```typescript
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  usedSources: string[]
  isStreaming: boolean
  isError?: boolean
  statusText?: string
  reasoningTrail: string[]
}
```

- [ ] **Step 2: 三处 `ChatMessage` 对象构造都补上 `reasoningTrail: []`**

第一处，`fetchSessionMessages` 加载历史会话时的映射（大约在 `useEffect` 里，`turns.map((turn) => ({...}))` 那段）：

```typescript
            messages: turns.map((turn) => ({
              id: createId(),
              role: turn.role === 'assistant' ? 'assistant' : 'user',
              text: turn.content,
              usedSources: [],
              isStreaming: false,
              reasoningTrail: [],
            })),
```

第二处，`sendQuestion` 里构造 `userMessage`：

```typescript
      const userMessage: ChatMessage = {
        id: createId(),
        role: 'user',
        text: question,
        usedSources: [],
        isStreaming: false,
        reasoningTrail: [],
      }
```

第三处，同一个函数里构造 `assistantMessage`：

```typescript
      const assistantMessage: ChatMessage = {
        id: assistantMessageId,
        role: 'assistant',
        text: '',
        usedSources: [],
        isStreaming: true,
        reasoningTrail: [],
      }
```

- [ ] **Step 3: 改 `tool_status` 事件处理器，清空前先把文字存进 `reasoningTrail`**

把：

```typescript
          } else if (parsed.type === 'tool_status') {
            const status = parsed as AgentToolStatusEvent
            // 工具调用轮之前可能出现的前置说明文字（比如"让我查一下。"）
            // 不是最终答案的一部分——tool_status 事件到达就说明这一轮
            // 结束、要开始/继续执行工具了，把已经显示的文字清空，重新
            // 露出"正在查询"指示器，而不是让这段文字停留在气泡里、
            // 之后又被 final 事件悄悄覆盖掉。
            patchAssistantMessage({ text: '', statusText: status.text })
```

改成：

```typescript
          } else if (parsed.type === 'tool_status') {
            const status = parsed as AgentToolStatusEvent
            // 工具调用轮之前可能出现的前置说明文字（比如"让我查一下。"）
            // 不是最终答案的一部分——tool_status 事件到达就说明这一轮
            // 结束、要开始/继续执行工具了，把已经显示的文字挪进
            // reasoningTrail（供用户按需展开查看推理过程），再清空、
            // 露出"正在查询"指示器，而不是让这段文字停留在气泡里、
            // 之后又被 final 事件悄悄覆盖掉，或者直接永久丢失。
            patchAssistantMessage((message) => ({
              ...message,
              text: '',
              statusText: status.text,
              reasoningTrail: message.text
                ? [...message.reasoningTrail, message.text]
                : message.reasoningTrail,
            }))
```

- [ ] **Step 4: typecheck**

Run: `cd frontend && npm run typecheck`
Expected: 无错误（`ChatMessage` 的三处构造点如果漏填 `reasoningTrail` 会在这一步报类型错误——这就是这次没有单元测试框架时，`reasoningTrail` 字段设计成必填而不是可选的价值：漏改会在编译期被抓到，不用等运行时）。

- [ ] **Step 5: 在 `MessageBubble.tsx` 里新增 `ReasoningTrail` 组件并接入**

把 `frontend/src/components/MessageBubble.tsx` 整个文件内容替换成：

```tsx
import type { ChatMessage } from '../hooks/useAgentChat'
import { MarkdownContent } from './MarkdownContent'
import { SourceCitations } from './SourceCitations'

interface MessageBubbleProps {
  message: ChatMessage
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[75%] rounded-card border px-4 py-3 shadow-soft ${
          isUser
            ? 'border-subtle bg-accent-pink text-on-accent'
            : message.isError
              ? 'border-status-error bg-card text-ink'
              : 'border-subtle bg-card text-ink'
        }`}
      >
        {message.text ? (
          isUser ? (
            <p className="whitespace-pre-wrap leading-relaxed">{message.text}</p>
          ) : (
            <MarkdownContent text={message.text} />
          )
        ) : message.isStreaming ? (
          <ThinkingIndicator statusText={message.statusText} />
        ) : null}
        {!isUser && message.reasoningTrail.length > 0 && (
          <ReasoningTrail steps={message.reasoningTrail} />
        )}
        {!isUser && !message.isStreaming && message.usedSources.length > 0 && (
          <SourceCitations sources={message.usedSources} />
        )}
      </div>
    </div>
  )
}

function ThinkingIndicator({ statusText }: { statusText?: string }) {
  return (
    <div className="flex items-center gap-2 py-1">
      {statusText && <span className="text-sm text-ink-soft">{statusText}</span>}
      <div className="flex items-center gap-1">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft motion-reduce:animate-none [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft motion-reduce:animate-none [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft motion-reduce:animate-none" />
      </div>
    </div>
  )
}

function ReasoningTrail({ steps }: { steps: string[] }) {
  return (
    <details className="mt-2 border-t border-subtle pt-2">
      <summary className="cursor-pointer text-xs text-ink-soft select-none">
        查看推理过程（{steps.length}步）
      </summary>
      <ol className="mt-1 space-y-1 text-xs text-ink-soft">
        {steps.map((step, index) => (
          <li key={index} className="whitespace-pre-wrap">
            {index + 1}. {step}
          </li>
        ))}
      </ol>
    </details>
  )
}
```

- [ ] **Step 6: typecheck**

Run: `cd frontend && npm run typecheck`
Expected: 无错误。

- [ ] **Step 7: 手动验证（这个前端项目没有配置测试框架，`npm run typecheck` 之外必须实际跑起来看）**

```powershell
powershell -File scripts/start-backend.ps1
powershell -File scripts/start-frontend.ps1
```

打开 `http://localhost:5173/`，问一个历史上会触发多轮工具调用的问题（比如"coke-cola公司有多少个订单"）。验证：
- 回答完成后，气泡下方出现"查看推理过程（N步）"的折叠区块（不是空的，也不是默认展开的）。
- 点开折叠区块，能看到每一轮工具调用前的叙述文字，按顺序排列。
- 主答案文本本身跟今天一样干净，没有混入这些叙述文字。
- 如果这次提问一轮就直接给出答案（没有触发过 `tool_status`），折叠区块不出现（`reasoningTrail` 为空数组时不渲染）。

- [ ] **Step 8: Commit**

```bash
git add frontend/src/hooks/useAgentChat.ts frontend/src/components/MessageBubble.tsx
git commit -m "feat(frontend): preserve per-round narration text as a collapsible reasoning trail"
```

---

## Self-Review Notes（写完计划后的自查记录）

- **Spec coverage**：spec 的"架构"一节（后端"最后陈述"机制、前端保留推理过程）→ Task 1/2/3 覆盖；"错误处理"一节四条 → Task 1/2 的测试分别覆盖"最后陈述调用失败/返回空文本"（对应今天行为不变）、"最后陈述调用成功"、"阶段1未选任何工具"（未改动，不需要新测试）；"测试"一节列的每一类都能在对应 Task 的测试步骤里找到（KV cache/召回相关内容不属于这份 spec，不在这份计划范围内）。
- **Placeholder scan**：无 TBD/TODO，所有代码块都是完整可运行的最终内容。
- **Type consistency**：`_run_final_answer_attempt`/`_run_final_answer_attempt_streaming` 的参数名、`_build_tool_call_round_result` 新签名在 Task 1/2 之间一致；`reasoningTrail: string[]` 在 Task 3 的接口定义和三处构造点、`MessageBubble` 消费处类型一致。
