# Planner 路径流式打字机效果 Design Spec

**日期：** 2026-08-23
**状态：** 已与用户逐段确认，待转 writing-plans 生成实现计划

## 背景

`app/agent/graph.py` 有两条问答执行路径：确定性检索路径（`retrieval_node` →
`responder_node`，纯向量/BM25 检索）和 Planner 自主规划路径（`planner_node`
↔ `tool_call_node` 多轮循环，会调用 `graph_query_tool`/
`structured_filter_query_tool` 查知识图谱）。`Settings.
agent_enable_autonomous_planning` 控制走哪条路径，语音请求
（`voice_response=True`）无论这个开关怎么配置都强制走确定性路径。

确定性路径的 `responder_node` 支持逐句流式推送（`on_answer_chunk` 回调 +
`stream_sentences()` 按句子边界切分 + 每句先过 `check_text` 轻量规则检查），
前端表现为打字机效果。Planner 路径完全没有这个能力——`app/agent/
planner.py` 里所有 LLM 调用都是一次性 `complete()`，最终答案要等全部
工具调用轮次+最后一轮生成完才通过 SSE 一次性发一条 `final` 事件，用户体验
上是"等一下，然后答案整段蹦出来"。

本次设计目标：让 Planner 路径的最终答案也能逐字流式输出，且多轮工具调用
期间给用户一个"正在查询"的状态反馈，而不是完全空白的等待。

## 决策记录（与用户逐条确认过）

1. **工具调用期间的可见性**：新增一个统一文案"正在查询相关信息..."的
   SSE 状态事件，不区分具体调用的是哪个工具。前端收到后在回答气泡位置
   显示一个带动画的 loading 文案，收到第一个真正的回答 delta 时切换成
   打字机效果。
2. **安全审查与流式的关系**：完全照搬确定性路径现有行为——边生成边按
   句子过一次 `check_text` 轻量规则检查（命中就把这一句换成兜底句再
   推送）；全文生成完后仍然要走一次完整的 `output_safety_node`（规则+
   泄露检测+语义审查三层）；如果最终判定不安全，`final` 事件的文本会
   被替换成兜底话术，已经推送给用户看过的 delta 内容收不回来——这个
   代价确定性路径已经"与产品方确认过"，Planner 路径接受同样的代价，
   不新增额外的撤回/编辑机制。
3. **一轮内文本先出现、后又改调工具的边界情况**：模型在一轮里通常从第
   一个增量块就能看出是要调工具还是直接回答（要么带 `tool_calls` 字段
   要么带 `content` 字段），但协议上不排除模型先输出一段文本再中途改
   调工具。这种情况下已经推送出去的文本不撤回——不引入缓冲/回滚机制，
   所有轮次的文本增量都是"来了就转发"，工具调用是否触发是独立判断的
   另一件事。这个决策的直接后果是实现可以简化：不需要"先攒 N 个 chunk
   再决定要不要开始转发"这种预判逻辑。

## 架构总览

新增一个**可选**的 provider 能力 `stream_complete_with_tools`，和现有
`stream_complete` 一样用 `hasattr()` 鸭子类型检测，不是 `Provider`
Protocol 的强制方法。Planner 每一轮推理时，如果当前配置的 LLM provider
支持这个新能力、且 `on_answer_chunk` 非空（即文字请求，语音请求从不会
走到 Planner），就走新的流式变体；否则透明回退到现有的一次性
`run_planner_turn()`，行为完全不变——**这次改动对不支持新能力的 provider
或语音请求是纯粹的无操作（no-op）**。

数据流示例（"Coca-Cola有多少个产品"）：

1. Planner 第 1 轮流式调用：模型决定调用 `graph_query_tool`，这一轮
   几乎不产生正文文本（工具调用轮通常没有前置说明文字）；流结束后拿到
   完整的 `tool_calls`。执行完工具调用后推一条 `tool_status` 事件，
   进入第 2 轮。
2. Planner 第 2 轮流式调用：模型直接输出正文文本，每个文本增量块一到
   就立刻转发给前端（打字机效果）。
3. 全文生成完后走完整语义安全审查，同确定性路径。

## 组件 1：Provider 层（`app/providers/base.py` + `app/providers/
openai_compatible.py` + `app/providers/registry.py`）

### `app/providers/base.py` 新增类型

```python
@dataclass(frozen=True)
class ProviderStreamChunk:
    text: str = ""
    tool_calls: list[ToolCall] | None = None
```

`text` 是本次增量文本，可能为空字符串（不代表流结束，只是这个 chunk
没有文本）。`tool_calls` 只会出现在流的最后一个 chunk 上，且是完整重建
好的列表（不是分片）——调用方不需要关心 OpenAI 协议里工具调用分片拼接
的细节，只需要判断 `chunk.tool_calls is not None` 来知道这一轮是否
请求了工具调用。

`Provider` Protocol 不新增这个方法（保持和 `stream_complete` 一样的
可选/鸭子类型约定）。

### `app/providers/openai_compatible.py` 新增方法

```python
async def stream_complete_with_tools(
    self, request: ProviderRequest
) -> AsyncIterator[ProviderStreamChunk]:
    """流式生成 + 支持工具调用：跟 stream_complete() 共用同一套 SSE 循环，
    额外处理 delta.tool_calls 的增量拼接。

    OpenAI 协议里流式场景下的工具调用按 index 分片到达：第一个携带该
    index 的分片通常带 id/type/function.name，此后同一个 index 的分片
    只携带 function.arguments 的字符串片段，要按到达顺序拼接。id/name
    只要出现过就不会再变，用字典按 index 累积即可。

    yield 的是「文本增量」和「工具调用（只在最后一个 chunk 上，且是完整
    重建好的）」这两种事件的合流。
    """
    payload = self._base_payload(request)
    payload["stream"] = True
    if request.tools:
        payload["tools"] = request.tools
    if request.tool_choice:
        payload["tool_choice"] = request.tool_choice

    # index -> {"id": str, "name": str, "arguments": str}（arguments 增量拼接）
    pending_tool_calls: dict[int, dict[str, str]] = {}

    async with self._client.stream(
        "POST",
        f"{self._base_url}/chat/completions",
        headers=self._headers(),
        json=payload,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk["choices"][0]["delta"]

            text = delta.get("content")
            if text:
                yield ProviderStreamChunk(text=text)

            for tc_delta in delta.get("tool_calls") or []:
                index = tc_delta["index"]
                entry = pending_tool_calls.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                if tc_delta.get("id"):
                    entry["id"] = tc_delta["id"]
                function_delta = tc_delta.get("function") or {}
                if function_delta.get("name"):
                    entry["name"] = function_delta["name"]
                if function_delta.get("arguments"):
                    entry["arguments"] += function_delta["arguments"]

    if pending_tool_calls:
        tool_calls = [
            ToolCall(id=entry["id"], name=entry["name"], arguments=entry["arguments"])
            for _, entry in sorted(pending_tool_calls.items())
        ]
        yield ProviderStreamChunk(tool_calls=tool_calls)
```

需要 `from app.providers.base import ProviderStreamChunk` 加入该文件的
import（`ToolCall`/`ProviderRequest`/`ProviderResult` 已经导入）。

### `app/providers/registry.py` 新增方法

```python
def supports_tool_streaming(self, capability: ProviderCapability, provider_name: str) -> bool:
    """跟 supports_streaming() 同一个模式：stream_complete_with_tools
    不是 Provider 协议的必需方法，调用方要先查一下再决定走流式还是
    一次性 complete()。"""
    provider = self._providers.get((capability, provider_name))
    return provider is not None and hasattr(provider, "stream_complete_with_tools")

async def stream_with_tools(
    self,
    capability: ProviderCapability,
    request: ProviderRequest,
    *,
    provider_name: str,
) -> AsyncIterator[ProviderStreamChunk]:
    provider = self._providers.get((capability, provider_name))
    if provider is None:
        raise KeyError(
            f"no provider registered for capability={capability!r} "
            f"name={provider_name!r}"
        )
    async for chunk in provider.stream_complete_with_tools(request):
        yield chunk
```

## 组件 2：安全兜底句常量搬家（`app/safety/rules.py`）

`_LITE_SAFETY_FALLBACK_SENTENCE`（当前定义在 `app/agent/graph.py:66`，
值 `"（该部分内容因安全检查被过滤。）"`）需要同时被 `graph.py`（确定性
路径的 `responder_node`）和 `planner.py`（新的流式变体）使用——但
`graph.py` 已经 `from app.agent.planner import route_after_planner,
run_planner_turn, run_tool_calls`，`planner.py` 反过来导入 `graph.py`
的常量会造成循环 import。

这正是 `app/safety/rules.py` 里 `UNSAFE_INPUT_MESSAGE`/
`UNSAFE_OUTPUT_MESSAGE` 当初搬家的同一个理由（该文件顶部注释原话：
"原定义在 app/agent/graph.py，现搬到这里作为共享位置...此前两处各自
写一份有文案不一致的风险"）——照搬同样的处理方式：

- 把 `_LITE_SAFETY_FALLBACK_SENTENCE` 移到 `app/safety/rules.py`，去掉
  前导下划线（跨模块共享，不再是模块私有），改名
  `LITE_SAFETY_FALLBACK_SENTENCE`。
- `app/agent/graph.py` 删除本地定义，改为
  `from app.safety.rules import LITE_SAFETY_FALLBACK_SENTENCE`，
  第 432 行的用法从 `_LITE_SAFETY_FALLBACK_SENTENCE` 改成
  `LITE_SAFETY_FALLBACK_SENTENCE`。
- `app/agent/planner.py` 同样 `from app.safety.rules import
  LITE_SAFETY_FALLBACK_SENTENCE, check_text`（`check_text` 也要新增
  import，`planner.py` 目前没有导入安全模块）。

## 组件 3：Planner 层（`app/agent/planner.py`）

### 提取共用的"这一轮有工具调用"分支逻辑

`run_planner_turn`（现有）和新的流式变体在"这一轮模型请求了工具调用"
时要做完全一样的事（往 `messages` 追加带 `tool_calls` 的 assistant
消息、构造 `pending_tool_calls` 返回值、检查轮次上限），提取成共用
helper 避免重复：

```python
def _build_tool_call_round_result(
    messages: list[dict[str, Any]],
    answer_text: str,
    tool_calls: list[ToolCall],
    *,
    round_num: int,
    max_tool_call_rounds: int,
) -> dict[str, Any]:
    if round_num >= max_tool_call_rounds:
        return {"planner_gave_up": True}
    messages = [*messages, {
        "role": "assistant",
        "content": answer_text or None,
        "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in tool_calls
        ],
    }]
    return {
        "planner_messages": messages,
        "pending_tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
        ],
    }
```

`run_planner_turn` 现有的 `if result.tool_calls:` 分支改成调用这个
helper（`answer_text` 传 `result.text`，`tool_calls` 传
`result.tool_calls`）——这一步是纯重构，不改变现有函数的对外行为，
需要现有测试全绿来兜底。

### 新增 `run_planner_turn_streaming`

```python
async def _split_stream_text_and_tool_calls(
    raw_stream: AsyncIterator[ProviderStreamChunk],
    tool_calls_box: list[list[ToolCall] | None],
) -> AsyncIterator[str]:
    """把 provider 流拆成两路：文本增量原样 yield 出去供 stream_sentences()
    消费；工具调用（如果有）写进 tool_calls_box[0]，供调用方在这个生成器
    耗尽后读取——用长度为 1 的列表当"可写引用"，闭包不能直接对外层局部
    变量重新赋值。
    """
    async for chunk in raw_stream:
        if chunk.text:
            yield chunk.text
        if chunk.tool_calls is not None:
            tool_calls_box[0] = chunk.tool_calls


async def run_planner_turn_streaming(
    state: dict[str, Any],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    max_tool_call_rounds: int,
    banned_terms: list[str] | None,
    on_answer_chunk: Callable[[str], Awaitable[None]],
    on_tool_status: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """run_planner_turn 的流式版本：语义完全一致（同样的轮次上限检查、
    同样的 planner_messages 追加规则），区别只是这一轮的文本用
    stream_complete_with_tools() 边生成边推送，而不是一次性拿到
    完整文本。见 docs/superpowers/specs/2026-08-23-
    planner-streaming-typewriter-design.md。
    """
    messages = list(state.get("planner_messages", []))
    round_num = state.get("tool_call_round", 0)

    raw_stream = llm_registry.stream_with_tools(
        ProviderCapability.LLM,
        ProviderRequest(messages=messages, tools=_TOOL_SCHEMAS, tool_choice="auto"),
        provider_name=llm_provider_name,
    )
    tool_calls_box: list[list[ToolCall] | None] = [None]
    text_stream = _split_stream_text_and_tool_calls(raw_stream, tool_calls_box)

    sent_sentences: list[str] = []
    async for sentence in stream_sentences(text_stream):
        safety_result = check_text(sentence, banned_terms=banned_terms, include_email=False)
        safe_sentence = sentence if safety_result.is_safe else LITE_SAFETY_FALLBACK_SENTENCE
        await on_answer_chunk(safe_sentence)
        sent_sentences.append(safe_sentence)
    full_text = "".join(sent_sentences)

    tool_calls = tool_calls_box[0]
    if tool_calls:
        result = _build_tool_call_round_result(
            messages, full_text, tool_calls,
            round_num=round_num, max_tool_call_rounds=max_tool_call_rounds,
        )
        if not result.get("planner_gave_up"):
            await on_tool_status()
        return result

    messages = [*messages, {"role": "assistant", "content": full_text}]
    return {
        "planner_messages": messages,
        "answer_text": full_text,
        "planner_gave_up": False,
    }
```

需要新增的 import：`from typing import AsyncIterator, Awaitable, Callable`
（`Callable`/`Awaitable` 目前 `planner.py` 没有导入过）、
`from app.providers.base import ProviderStreamChunk, ToolCall`、
`from app.safety.rules import LITE_SAFETY_FALLBACK_SENTENCE, check_text`、
`from app.voice.streaming_responder import stream_sentences`。

**注意 `on_tool_status()` 调用时机**：只在确认这一轮真的会继续执行工具
调用（没有触发 `planner_gave_up`）时才推状态事件——轮次耗尽直接放弃的
场景不应该让用户以为"还在查"。

## 组件 4：graph.py 接线

### `planner_node` 改成先判断走不走流式

```python
async def planner_node(state: AgentState) -> dict[str, Any]:
    messages = state.get("planner_messages")
    if not messages:
        messages = [...]  # 现有构造逻辑不变
    state_with_messages = {**state, "planner_messages": messages}
    if on_answer_chunk is not None and llm_registry.supports_tool_streaming(
        ProviderCapability.LLM, llm_provider_name
    ):
        return await run_planner_turn_streaming(
            state_with_messages,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            max_tool_call_rounds=max_tool_call_rounds,
            banned_terms=banned_terms,
            on_answer_chunk=on_answer_chunk,
            on_tool_status=on_tool_status,
        )
    return await run_planner_turn(
        state_with_messages,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        max_tool_call_rounds=max_tool_call_rounds,
    )
```

（`banned_terms`、`on_answer_chunk` 已经是 `build_agent_graph` 的现有
闭包变量；`on_tool_status` 是新增的。）

### `build_agent_graph` 新增参数

```python
def build_agent_graph(
    ...,
    on_answer_chunk: Callable[[str], Awaitable[None]] | None = None,
    on_tool_status: Callable[[], Awaitable[None]] | None = None,
    ...,
) -> CompiledStateGraph:
```

`on_tool_status` 放在 `on_answer_chunk` 紧后面，文档字符串补一句说明：
"Planner 路径专用，工具调用轮次结束、确认要继续执行工具时调用一次，
用于给前端展示'正在查询'状态；确定性路径和不支持流式的 provider 不会
触发这个回调。"

### `responder_node` 的常量引用更新

第 432 行 `_LITE_SAFETY_FALLBACK_SENTENCE` → `LITE_SAFETY_FALLBACK_SENTENCE`
（导入来源变化，见组件 2）。

## 组件 5：路由层（`app/api/agent_routes.py`）

`event_stream()` 内新增一个闭包，跟现有 `on_text_chunk` 同一个模式：

```python
async def on_tool_status() -> None:
    body = json.dumps({"type": "tool_status", "text": "正在查询相关信息..."}, ensure_ascii=False)
    await queue.put(body)
```

`build_agent_graph(...)` 调用处新增
`on_tool_status=on_tool_status if not payload.voice_response else None`
——语义上语音请求不需要这个回调（Planner 本来就不会跑），显式传 `None`
比"反正传了也用不到"更清楚地表达意图。

## 组件 6：前端（`frontend/src/hooks/useAgentChat.ts` + 类型定义 + 渲染组件）

- SSE 事件类型定义处新增 `AgentToolStatusEvent { type: 'tool_status';
  text: string }`，加入 `AgentEvent` 联合类型。
- `ChatMessage` 类型新增可选字段 `statusText?: string`。
- SSE 处理循环新增分支：
  ```ts
  } else if (parsed.type === 'tool_status') {
    const status = parsed as AgentToolStatusEvent
    patchAssistantMessage({ statusText: status.text })
  }
  ```
- `delta` 分支追加清空：`patchAssistantMessage((message) => ({ ...message, text: message.text + delta.text, statusText: undefined }))`
- `final` 分支同样确保 `statusText: undefined`（防御性清空，处理"根本没有
  triggerる tool_status 但也没有走流式"的非流式回退场景）。
- 渲染消息气泡的组件（需要在实现阶段定位具体文件，大概率是
  `ChatMessage`/`MessageBubble` 一类组件）：`statusText` 有值且 `text`
  为空时，显示一个带简单动画（比如三个跳动的点，复用项目里如果已有的
  loading 组件样式）的状态文案，替代当前"完全空白直到出字"的等待态。

## 测试策略

- `tests/providers/test_streaming.py`（或新建
  `test_streaming_with_tools.py`，复用同文件里 `_sse_body`-类的构造
  helper，但要扩展支持 `tool_calls` 分片）：
  - 纯文本流：不应该出现 `tool_calls`，`ProviderStreamChunk.text`
    序列拼起来等于完整回答。
  - 纯工具调用流：`text` 全程为空/极少，最后一个 chunk 的 `tool_calls`
    重建正确（覆盖单个工具调用、并发多个工具调用两种分片交错场景）。
  - 混合场景：一段文本后紧跟工具调用（对应决策 3 的边界情况）。
- `tests/agent/test_planner.py`：
  - `run_planner_turn_streaming` 工具调用轮：`on_answer_chunk` 收到的
    文本、`pending_tool_calls`、`planner_messages` 里 assistant 消息的
    `tool_calls` 字段，跟非流式 `run_planner_turn` 在同等输入下的行为
    做对照断言。
  - 直接回答轮：逐句调用 `on_answer_chunk`；命中 `banned_terms` 时替换
    成 `LITE_SAFETY_FALLBACK_SENTENCE`。
  - 轮次耗尽（`round_num >= max_tool_call_rounds`）时不调用
    `on_tool_status()`。
  - `_build_tool_call_round_result` 的重构回归测试：`run_planner_turn`
    重构前后行为一致（现有测试文件里的既有用例应该在重构后原样通过，
    不需要改断言）。
- `tests/agent/test_graph.py`：
  - Planner+流式 provider 时的端到端场景：SSE 事件序列包含
    `tool_status` → 若干 `delta` → `final`。
  - Provider 不支持 `stream_complete_with_tools` 时透明回退到非流式
    （用一个只有 `complete()`、没有 `stream_complete_with_tools` 的
    Fake provider，验证行为跟改动前完全一致）。
- `tests/api/test_agent_chat_routes.py`：一条端到端集成测试，走真实的
  `/agent/chat` 端点，断言收到的 SSE 事件里出现 `tool_status` 类型。
- 前端：项目没有自动化测试框架，用 `tsc --noEmit` 校验 + 手工验证步骤
  （无浏览器自动化，按预期结果描述形式记录，跟本仓库其它前端改动的
  验证方式一致）。

## 错误处理

- 流式请求中途网络异常：跟现有 `stream_complete()` 一样直接向上抛，
  `event_stream()` 外层已有的 `try/finally` 保证 `queue.put(None)` 会
  发出去结束 SSE，前端现有异常兜底文案接住，不新增专门的重试/降级逻辑
  ——静态流式路径现在也没有，保持行为一致。
- 工具调用分片拼接后 `arguments` 不是合法 JSON：沿用 `run_tool_calls()`
  现有的"解析失败就回填 `{"error": "arguments 不是合法 JSON"}` 观察
  结果，不抛异常"策略，不需要新代码——`_build_tool_call_round_result`
  只负责把（可能不合法的）`arguments` 字符串原样放进
  `pending_tool_calls`，真正解析是 `tool_call_node`/`run_tool_calls`
  的职责，这一层完全不变。

## 范围外（本次不做）

- 不改动 `stream_complete()`（纯文本流式，静态路径/语音路径继续用它，
  互不影响）。
- 不新增"重新生成"/"编辑已流式内容"这类撤回机制（决策 2/3 已明确接受
  "已推送不撤回"的代价）。
- `tool_status` 文案固定为"正在查询相关信息..."，不按具体工具名区分
  （决策 1）。
- 除 `openai_compatible.py` 外不新增其它 provider 的
  `stream_complete_with_tools` 实现——这是目前唯一有实现的 LLM
  provider。

## 自查

**1. 决策覆盖：** 三条决策（工具调用可见性、安全审查行为、混合边界
情况）在架构总览和各组件设计里都有对应落地，没有遗漏。

**2. 占位符扫描：** 通读全文，除"渲染消息气泡的组件需要在实现阶段定位
具体文件"这一处显式标注为"实现阶段需要做的事"（不是含糊其辞，是诚实
说明这份 spec 没有去翻前端组件目录确认具体文件名，留给 writing-plans
阶段去定位——这是设计文档和实现计划的边界，不是缺陷），其余每个组件都
给了完整代码或明确的编辑指令，没有"合理处理"这类空话。

**3. 内部一致性：** `ProviderStreamChunk`/`run_planner_turn_streaming`/
`on_tool_status`/`LITE_SAFETY_FALLBACK_SENTENCE` 等命名在各组件之间
使用一致；`_build_tool_call_round_result` 的签名和 `run_planner_turn`
现有分支、`run_planner_turn_streaming` 两处调用方式对得上。

没有发现需要修的问题。
