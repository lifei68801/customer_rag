# Planner 工具调用轮次耗尽时的兜底体验设计

## 背景

2026-08-25 复现了这样一个问题："coke-cola公司有多少个订单"这个问题，Planner 反复试错关系方向和锚点类型，最终耗尽 `max_tool_call_rounds`（默认3轮）后，前端只显示一句写死的"抱歉，暂时没有找到确切答案，已为您转接人工客服处理"，用户看不到任何有价值的信息——即便这几轮工具调用里，Planner 实际上已经通过关系遍历查到过一些真实数据。

深入代码后确认这是两个独立、叠加在一起的问题：

1. **后端**：`app/agent/planner.py` 的 `_build_tool_call_round_result` 在轮次耗尽且 LLM 仍要求调用工具时，直接返回 `{"planner_gave_up": True}`——不追加消息、不执行工具、也不给 LLM 任何"基于已有信息总结一下"的机会。`app/agent/graph.py` 的 `fallback_node` 命中后直接吐出静态文案，`planner_messages` 里已经积累的真实工具调用结果被完全丢弃，从不出现在最终回复里。
2. **前端**：`frontend/src/hooks/useAgentChat.ts` 的 `tool_status` 事件处理器在每一轮工具调用开始前，会把当前已经流式显示的文字（LLM 在决定调用工具前的叙述，比如"让我先查一下xxx"）清空，换成"正在查询..."指示器。这是有意设计（避免多轮叙述堆在最终答案气泡里显得凌乱），但代价是：除了最后一轮，之前每一轮 LLM 的推理过程都被丢弃、用户完全看不到，哪怕这些叙述里包含"我看到了问题所在""这个数据没有区分度"这类真正有诊断价值的内容。

这两个问题共同导致了本次复现里最差的那种体验：用户看到"正在查询..."反复闪烁几次，然后突然冒出一句跟提问内容毫无关联的转人工文案。

这份设计只解决"轮次耗尽/查询卡住时该怎么体面收场"，不改变"为什么会卡住"（多跳关系构造对 LLM 本身更容易出错、`max_tool_call_rounds` 默认值是否够用）——这些留在 Non-Goals 里说明。

## 目标

- 轮次耗尽时，Planner 不再无条件放弃：多做一次不带工具调用权限的"最后陈述"，让 LLM 基于已经查到的全部信息，尽力给用户一个有帮助的回答（有结论给结论，没有结论就说清楚已知情况和限制），而不是套用一句放之四海皆准的转人工文案。
- 这次"最后陈述"产出的内容，必须经过跟任何正常回答完全一样的规则+语义安全审查——不能因为它诞生于"兜底路径"就降低审查标准。
- 只有当"最后陈述"本身也失败（LLM 调用出错，或返回空文本）时，才退回今天的行为（静态文案+创建人工工单）——不引入比今天更差的失败模式。
- 前端保留每一轮工具调用前被清空的叙述文字，做成默认折叠的"推理过程"区域，而不是永久丢弃——不管这一轮最终是成功给出结论，还是走到"最后陈述"分支。

## 架构

### 后端：轮次耗尽时的"最后陈述"

改动全部在 `app/agent/planner.py` 内，`run_planner_turn`（非流式）和 `run_planner_turn_streaming`（流式）两处分别处理，延续这个文件里"两个函数逻辑完全对应、各自独立维护"的既有风格。

**触发时机不变**：仍然是 `round_num >= max_tool_call_rounds` 且这一轮 LLM 的响应里带 `tool_calls`。

**新行为**：命中这个条件时，不再直接放弃，而是：

1. 用**这一轮开始前的 `messages`**（不包含这次被拒绝的 tool_calls 请求——跟今天保持一致，避免把"申请了工具调用但没执行"的 assistant 消息留在历史里，破坏后续对话结构）
2. 追加一条新的 `system` 角色指令消息（原文见下）
3. 重新调用一次 LLM，**这次请求不带 `tools` 参数**——结构上让 LLM 没有办法再申请任何工具调用，不依赖 provider 是否正确支持 `tool_choice="none"` 语义

**追加的 system 指令原文**：

> 你已经达到本轮对话可用的工具调用次数上限，不能再调用任何工具了。请基于你目前已经查询到的全部信息，尽力给用户一个有帮助的回答：如果已经有明确的结论或数字，直接给出；如果现有信息不足以给出确定结论，清楚说明你目前掌握的情况、以及为什么无法进一步确认（比如某个维度在当前数据里没有区分度、或者查询本身没有找到匹配结果），不要用套话搪塞，也绝不能编造没有查到的数据。

**成功/失败判定与后续路由**：

- 这次调用返回**非空文本** → 按照跟"LLM 主动决定不再调工具、直接给出最终答案"完全一样的返回形状处理：`planner_messages`（追加这轮的 assistant 消息）、`answer_text`、`planner_gave_up: False`。**不需要改动 `app/agent/graph.py` 的图结构/路由**——`route_after_planner` 会按现有逻辑把它送进 `planner_responder_node → output_safety_node`，自动获得跟任何正常回答完全一样的规则+语义双重安全审查；`planner_responder_node` 本身已经写明"不再发起第二次 LLM 调用"，这次多出来的调用正好填上这个位置，接口形状不冲突。因为 `planner_gave_up` 是 `False`，也不会触发 `create_ticket_node`——用户拿到了真实回答，就不该自动创建人工工单。
- 这次调用**返回空文本，或者调用本身抛异常**（网络错误、provider 报错；防御性地，即便请求没带 `tools`，万一某个 provider 仍返回了非空 `tool_calls`，也一律忽略 `tool_calls`、只看 `text` 是否非空）→ 保留原有的 `{"planner_gave_up": True}"`，走今天完全一样的路径：`fallback_node` 静态文案 + `create_ticket_node` 创建人工工单。这是这个设计"下限不比今天差"的保证。

**流式路径的额外要求**（`run_planner_turn_streaming`）：这次"最后陈述"调用同样通过 `stream_with_tools`（不传 `tools`）+ 现有的 `stream_sentences`/`check_text` 逐句安全替换机制，经同一个 `on_answer_chunk` 回调继续往前端推送——用户体验上是从"查询过程"无缝过渡到"总结陈述"，而不是先看到一段查询叙述、中间断一下、再冒出一句无关的静态文案。**不调用 `on_tool_status()`**：延续现有代码里"轮次耗尽直接放弃的场景不应该让用户以为还在查"这条注释的原则，这次"最后陈述"同样不是"还在查"，不该触发状态提示。

**不引入新的配置开关**：这个改动本身是"失败时退化到今天的行为"，风险面小，不额外增加一个配置项。

**`max_tool_call_rounds` 默认值（3）保持不变**：本次设计范围不包括"提高一次性查询构造成功率"，见 Non-Goals。

### 前端：保留推理过程

这部分完全独立于后端改动，也不需要后端配合——今天后端本来就已经把每一轮工具调用前的叙述文字通过 `delta` 事件发给了前端，只是前端自己在收到 `tool_status` 时把它扔掉了。

**`frontend/src/hooks/useAgentChat.ts`**：

- `ChatMessage` 接口新增字段：`reasoningTrail: string[]`
- 新建消息时初始化为 `reasoningTrail: []`
- `tool_status` 事件处理器：清空 `text` 之前，如果当前 `message.text` 非空，先把它 push 进 `reasoningTrail`，再清空 `text`、设置新的 `statusText`。伪代码：
  ```ts
  } else if (parsed.type === 'tool_status') {
    const status = parsed as AgentToolStatusEvent
    patchAssistantMessage((message) => ({
      ...message,
      text: '',
      statusText: status.text,
      reasoningTrail: message.text ? [...message.reasoningTrail, message.text] : message.reasoningTrail,
    }))
  }
  ```
- `final` 事件处理器不变——最后一轮的文本仍然按 `final.text` 覆盖式设置到 `text`，不追加进 `reasoningTrail`（这是这一轮真正的答案，不是被丢弃的叙述）。

**`frontend/src/components/MessageBubble.tsx`**：

- 主答案气泡下方，`message.reasoningTrail.length > 0` 时渲染一个默认折叠的展开区块，标题类似"查看推理过程（{N}步）"，样式弱化（小字号、次要文字颜色），点开后按顺序列出 `reasoningTrail` 里每一段文字。
- 不影响主答案的渲染逻辑（`message.text` 的展示方式完全不变）——原有"最终答案气泡要干净"的设计初衷保留，只是把原本永久丢弃的信息挪到一个不打扰阅读的、可选查看的位置，而不是删除它。

**跟后端改动的组合效果**：即便"最后陈述"没能给出确定结论（比如遇到"公司维度在当前数据里没有区分度"这类情况），用户至少能展开推理过程，看到 LLM 具体卡在哪一步、做了什么判断——而不是像今天这样，所有中间过程全部消失，只剩一句无关的静态文案。

## 错误处理

- "最后陈述"调用失败（网络错误、provider 报错、返回空文本）→ 退回今天的 `planner_gave_up: True` 行为，静态文案 + 人工工单，不引入新的失败模式。
- 流式路径里某句话被 `check_text` 规则判定不安全 → 复用现有的逐句替换机制（换成 `LITE_SAFETY_FALLBACK_SENTENCE`），不需要新逻辑。
- `output_safety_node` 的语义安全审查判定"最后陈述"内容不安全 → 这是已有机制自动生效的部分（所有 `fallback_triggered=False` 的回答都会经过这一步），不需要为这个新路径单独处理。
- 前端：`reasoningTrail` 为空数组是正常状态（问题一轮就答完，没有触发过 `tool_status`），折叠区块不渲染，不是错误。

## 测试

**后端**：
- `run_planner_turn`：新增测试覆盖"轮次耗尽 + 最后陈述调用成功 → `planner_gave_up=False` + 返回文本"和"轮次耗尽 + 最后陈述调用失败/空文本 → 仍然 `planner_gave_up=True`"两个分支，用 fake LLM provider 控制第二次调用的返回值。
- 新增测试验证"最后陈述"这次调用的 `ProviderRequest.tools` 确实是 `None`/未设置——防止将来有人不小心把 `tools` 加回去，导致又能无限循环申请工具。
- `run_planner_turn_streaming`：新增测试验证这个分支不会调用 `on_tool_status()`；验证流式输出的文本经过 `on_answer_chunk` 正常推送。
- 需要检查 `tests/api/test_agent_chat_routes.py` 里已有的、断言"轮次耗尽→静态转人工文案"行为的测试——这些需要根据新行为更新（预期的行为变化，不是回归）。写实现计划时要先确认这些测试具体断言的是什么、哪些需要改。

**前端**：
- `useAgentChat.ts` 的 `tool_status` 处理逻辑：验证多轮工具调用后 `reasoningTrail` 正确累积了每一轮清空前的文本，且顺序正确。
- 验证 `final` 事件不会把最后一段文本错误地也塞进 `reasoningTrail`。
- `MessageBubble.tsx`：验证 `reasoningTrail` 为空时不渲染折叠区块；非空时渲染且默认折叠。

## Non-Goals（不在这次设计范围内）

- **不提高多跳关系构造的一次性成功率**——比如给 system prompt 加更具体的"2跳关系怎么判断方向"的例子或指导。这是另一个独立的问题（让 Planner 更少走到轮次耗尽这一步），跟这次"耗尽之后怎么体面收场"是两回事。
- **不调整 `max_tool_call_rounds` 默认值**——同上，这是"减少触发频率"的杠杆，不是这次设计的目标。
- **不在查询引擎层面识别"结果无区分度"**（比如 matched_count 接近或等于该 term_type 全量时给出警示）。这类检测本身有不小的复杂度和误判风险（"无区分度"对不同业务场景含义不同），且这次的"最后陈述"机制已经能让 LLM 自己在总结时用自然语言表达出"这个维度看起来没有区分度"这种判断，不需要在代码层面单独建一套检测逻辑。
- **不引入新的配置开关**，理由见架构一节。

## Global Constraints

- 后端改动只涉及 `app/agent/planner.py`；不改 `app/agent/graph.py` 的图结构/路由、不改 `app/agent/tools.py`、不改查询引擎（`app/graphrag/` 下任何文件）。
- "最后陈述"调用失败时的行为必须与今天完全一致（`planner_gave_up: True` → 静态文案 + 人工工单），不允许引入介于"正常成功"和"今天的失败态"之间的第三种模糊状态。
- "最后陈述"产出的文本必须经过 `output_safety_node` 的完整规则+语义审查（即 `fallback_triggered` 必须为 `False`），不允许绕过。
- 前端改动只涉及 `frontend/src/hooks/useAgentChat.ts` 和 `frontend/src/components/MessageBubble.tsx`；不改变 `message.text`/`final` 事件的既有渲染逻辑。
- 不新增配置开关，不调整 `max_tool_call_rounds` 默认值。
