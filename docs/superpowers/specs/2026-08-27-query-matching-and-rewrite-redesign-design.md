# 查询匹配统一 + 改写架构重设计

## 背景

2026-08-27 深挖"coke-cola公司有多少个订单"这条链路时，确认了两处独立的、真实发生过 bug 的结构性问题——不是措辞调整能根治的，需要重新设计数据流。

### 问题 1：两套互不通信的实体匹配实现

系统里有两处独立代码，各自回答"这段文本提到了哪个已知实体"这个同一个问题：

- `app/graphrag/term_guard.py::match_terms()`——在 Planner 推理**之前**无条件跑，用 `difflib.SequenceMatcher` 逐候选滑窗打分（阈值 0.75），命中的术语的一跳邻居会被强制注入系统上下文。
- `app/graphrag/ontology_recall.py::recall_ontology_candidates()`——只在 `structured_filter_query_tool.resolve_arguments()` 内跑，用 `longest_common_substring_score()`（最长公共连续子串长度 / 候选长度，带 `_MIN_OVERLAP_LENGTH` 下限）+ n-gram 预过滤打分排名，结果当作候选参考喂给深层参数生成 LLM。

两套算法对长短候选词的打分特性不同：`SequenceMatcher` 对长候选词的局部差异更敏感（容易把分数拉低），对短候选词的巧合重叠更宽容。这直接导致了这次的真实 bug——用户输入"coke-cola"时，`match_terms()` 用 `SequenceMatcher` 算出"Coca-Cola"（9字符，公司）相似度约 0.55（不过阈值），"Cola"（4字符，产品）相似度恰好压线 0.75（过阈值）——于是 `term_guard` 只注入了错误实体"Cola"产品的上下文（本例中还带出了 996 条无关订单号），把 Planner 从第一个 token 起就带偏，尽管 `recall_ontology_candidates()` 后来是能正确同时召回两个实体的（已通过本次会话早前的另一处修复——大小写不敏感——验证）。

两处算法不统一，意味着同一段文本在两处会得出不一致、甚至矛盾的匹配结论，且系统里没有任何一处会发现/警告这种矛盾。

已排除的合并方向：让两处共享同一次匹配计算（“算一次、都复用”）。这条路径已确认不可行——`recall_ontology_candidates()` 的完整输入是 `f"{query_intent}\n{context.question}"`，而 `query_intent` 要等 Planner 推理完成、生成 tool_calls 参数之后才存在，这个时间点严格晚于 `term_guard_node`（在 Planner 推理之前跑）已经执行完毕的时刻——`term_guard` 该做“共享计算”的那一刻，`recall_ontology_candidates()` 的完整输入根本还不存在。折中的“缓存 term_guard 对 `context.question` 的匹配结果、`recall` 复用后再对 `query_intent` 增量扫描”方案，只能省掉重复扫描 `context.question` 这一次开销（`query_intent` 部分仍要单独扫），收益小、复杂度增（需要合并两次结果），不予采用。

### 问题 2：改写机制分散、职责不清，是"计数意图丢失"系列 bug 的结构性根源

`structured_filter_query_tool` 的参数生成拆成两次独立 LLM 调用（渐进式披露设计，见 `2026-08-25-progressive-disclosure-recall-augmented-params-design.md`）：Planner（环节 2）生成一段自由文本 `query_intent`，深层参数生成 LLM（环节 3）只看到这段转述文本（加上本次会话早前作为兜底加入的 `context.question` 原文）来决定 `anchor`/`constraints`。`query_intent` 当前的 schema 描述鼓励"用自然语言描述这次想查询的内容"——一个默认执行"概括/转述"的开放式指令，没有"默认不改写"这个基线，也没有"当前问题里已经出现的措辞必须原样保留"这条约束。本次会话实测反复观察到：Planner 把用户"coke-cola公司有多少个订单"这个明确的计数问题，转述成"查询产品 Cola 的信息"这类丢失了"多少个/订单"关键信息的版本，导致下游深层参数生成 LLM 选择了错误的 `anchor.name` 模式而不是计数模式。本次会话已经尝试过两次纯提示词层面的补救，均未能稳定生效——问题不在文案，在于机制完全依赖 LLM 的注意力/遵从度，没有结构性约束或校验。

同时确认：`app/qa/query_rewrite.py::rewrite_query()`（服务 `vector_search_tool` 文档检索的独立改写模块）是**完全独立、互不通信的第二套改写实现**——跟 `query_intent` 服务不同工具、各自维护一份"要不要改写、怎么改写"的判断，本质上是设计 A 那类"同一个问题被两套互不通信的实现各自解决一次"的结构性问题在改写这一侧的另一种表现。本次会话调试记录也证实 Planner 经常在**同一轮**同时调用这两个工具——两套改写各自独立运行，存在对同一个用户意图给出不一致改写结果的风险。

#### 一度考虑过、后来被推翻的方向：直接把完整对话历史喂给召回

受本地项目 `swiftagent`（`dev/3.0.19` 分支）ReAct 架构（`react/README.md`）的启发——它的多轮上下文关联完全不靠"改写"：`SandboxResultManager._load_chat_history()` 把完整历史消息数组原样恢复、拼进这一步的 `messages`，指代消解发生在 LLM 推理内部，没有显式的改写步骤——一度设想让 customer_rag 也照此思路，让 `structured_filter_query_tool` 的候选召回（`recall_ontology_candidates()`）和文档检索直接吃对话历史原文，取消 `query_intent`/`rewrite_query()` 这类改写。

这个方向被证明是错的：`recall_ontology_candidates()`/文档向量检索都是"用一段文本去匹配候选"的机制，如果直接喂入历史原文拼接，会连带召回大量跟本轮问题无关的旧话题实体/文档片段，稀释候选列表精度，导致下游深层参数生成 LLM 因为看到无关候选而构造出错误的查询要素——ReAct 架构不需要面对这个问题，是因为它的下游消费者（LLM 自己的推理）能够自己甄别历史里哪些相关、哪些不相关；但 `recall_ontology_candidates()`/向量检索这类**非 LLM 的文本匹配机制**做不到这种甄别，历史喂得越多、噪声越大。

**改写本身不能取消**——召回环节需要的始终是"一句聚焦、精简、已消解指代的话"，不是"一段历史"。真正该修的不是"要不要改写"，而是"改写这件事目前分散成两套互不通信的实现、且转述质量不可靠"。

#### 重新定位问题：两个性质不同的子问题被混在了一起

深挖下去，"改写"实际上被要求同时解决两个性质完全不同的问题，这是当前设计脆弱的根源：

1. **跨轮次指代消解**（"它还剩多少个"→"Coca-Cola还剩多少个"）——依赖历史，但结果在这一轮的处理过程中应该是稳定的，不会因为这一轮内部多查了几次工具而改变。
2. **同一轮内部，Planner 根据工具反馈调整措辞**（第一次查不到、换个问法再查）——这是 Planner 自适应探索的正常过程，不涉及历史指代，是根据*刚拿到的工具结果*调整下一步怎么问。

现状是这两件事被压在了同一个"Planner 生成 query_intent"的自由文本生成里，一次性、隐式地完成——没有拆开处理，也没有为其中任一部分提供结构性保障。

## 目标

- 统一两处实体匹配算法，消除"同一段文本两处给出矛盾结论"的可能性。
- 把"改写"拆成两层各自处理一个性质不同的子问题：跨轮次指代消解（一次性、稳定）与轮内转述保真（每轮、受工具反馈驱动，但不能丢失当前问题里的显式信息）。
- 让两个工具（`structured_filter_query_tool`/`vector_search_tool`）共享同一份指代消解结果，不再各自独立改写、可能给出不一致结论。
- 为"是否改写、改写是否保真"提供不依赖 LLM 自我报告的确定性核对。
- 顺带解决一个改写统一后才有条件做的相关问题：本轮问题如果和最近历史里已经问过且已回答的问题基本相同，给 Planner 一个可参考、但不强制的复用提示，减少不必要的重复工具调用。
- 不引入不必要的新基础设施；新增的一次 LLM 调用（历史指代消解）必须在没有可用历史时零成本跳过。

## 设计 A：统一实体匹配算法

### 现状代码

- `app/graphrag/term_matcher.py::_has_fuzzy_match()`（现状，本次会话已修复大小写不敏感）：
  ```python
  def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
      window = len(candidate)
      if window == 0 or len(text) < window:
          return False
      candidate_lower = candidate.lower()
      for i in range(len(text) - window + 1):
          span = text[i : i + window]
          ratio = difflib.SequenceMatcher(None, span.lower(), candidate_lower).ratio()
          if ratio >= threshold:
              return True
      return False
  ```
- `app/graphrag/ontology_recall.py::longest_common_substring_score()`（现状）：
  ```python
  def longest_common_substring_score(a: str, b: str) -> float:
      if not b:
          return 0.0
      overlap = _longest_common_substring_length(a, b)
      if overlap < _MIN_OVERLAP_LENGTH:
          return 0.0
      return overlap / len(b)
  ```

### 改动

在 `app/graphrag/term_matcher.py` 里，把 `_has_fuzzy_match()` 的打分算法从 `difflib.SequenceMatcher` 换成 `ontology_recall.py` 已有的 `longest_common_substring_score()`——`term_matcher.py` 改为依赖 `ontology_recall.py` 暴露这个函数（`ontology_recall.py` 是更晚加入的模块，`term_matcher.py` 更早存在；依赖方向定为 `term_matcher` 依赖 `ontology_recall`，因为后者的算法本次选定为统一后的标准实现，改动 `term_matcher.py` 一处即可，不用同时改两处保持同步）。

```python
# app/graphrag/term_matcher.py
from app.graphrag.ontology_recall import longest_common_substring_score

def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
    window = len(candidate)
    if window == 0 or len(text) < window:
        return False
    for i in range(len(text) - window + 1):
        span = text[i : i + window]
        # longest_common_substring_score 内部按候选长度归一化，
        # 语义上跟这里的 window==len(candidate) 天然对齐。
        if longest_common_substring_score(span, candidate) >= threshold:
            return True
    return False
```

`longest_common_substring_score()` 已经是大小写不敏感的实现（本次会话验证过），不需要额外改动。`match_terms()` 的精确匹配分支（`candidate.lower() in text_lower`）不受影响，继续保留。

**已知代价的迁移**：`term_matcher.py` 现有测试 `test_match_terms_known_false_positive_for_similar_short_error_codes`（E502/E503 在 0.75 阈值下误判为模糊命中）记录的是 `SequenceMatcher` 算法下的具体分数（0.75 恰好持平）。换算法后这条已知代价的具体数值可能变化（`longest_common_substring_score` 对短候选词的行为特性不同），需要重新计算 E502 相对 E503 的实际得分，更新测试注释和断言，不能直接照搬旧数值。

**阈值是否需要调整**：`_MIN_OVERLAP_LENGTH`（`ontology_recall.py` 内部常量）和 `fuzzy_threshold=0.75`（`term_matcher.py` 的默认参数）是两套独立的调节旋钮，服务不同目的——前者防止极短重叠虚高，后者是"整体多相似算命中"的总阈值。换算法后需要用现有测试用例（`tests/graphrag/test_term_matcher.py` 全部用例，含本次会话新增的 `coke-cola` 大小写场景）跑一遍回归，确认 0.75 这个阈值在新算法下是否仍然让所有既有场景保持预期行为；如果不行，允许调整默认阈值，但要在改动说明里写清楚新阈值下每条既有测试用例的实际得分，不能只改到测试变绿就停。

### 测试

- `tests/graphrag/test_term_matcher.py` 全量跑一遍，确认所有现有场景（含本次会话新增的大小写测试）行为不回归；`test_match_terms_known_false_positive_for_similar_short_error_codes` 按新算法下的实际得分重写断言和注释。
- 新增一条测试：用同一段文本（"coke-cola公司有多少个订单"）分别调用 `match_terms()` 和 `recall_ontology_candidates()`，断言两者对"Coca-Cola"这个实体的匹配结论一致（都命中或都不命中），作为"两处不再矛盾"这个设计目标的直接回归锚点。

## 设计 B：两层改写架构——历史指代消解（Layer 1）+ 轮内转述保真（Layer 2）

### 现状节点顺序（决定 Layer 1 只能插在哪）

`app/agent/graph.py` 里实际的节点顺序：

```
START → input_safety → clarification_check → term_guard → memory_recall
      → merge_after_parallel → planner（或 retrieval）→ ...
```

`memory_context_messages`（近期对话轮次，见 `app/memory/context_injection.py::inject_memory_context()`）由 `memory_recall_node` 产出，**排在 `term_guard` 之后**。Layer 1 需要这份历史作为输入，只能插在 `memory_recall` 之后、`merge_after_parallel` 之前——`term_guard` 因此继续基于原始 `state["question"]` 工作，不会吃到消歧后的问题。这不影响任何已验证过的 bug：`term_guard` 处理的是"文本里字面提到了哪个已知术语"，跟指代消解是两个不同维度的问题，本次会话追的具体 bug（`match_terms` 大小写敏感导致锚定错误实体）已经在设计 A 之外单独修复过，与 Layer 1 无关。

### Layer 1：历史指代消解（新节点，一次性，跨轮次稳定）

新增 `app/qa/query_rewrite.py::resolve_question()`——`app/qa/query_rewrite.py` 本来就是"改写"相关逻辑的落脚点，Layer 1 和收窄后的 `rewrite_query()`（见设计 C）放在同一个文件里，职责上是同一件事的两个阶段。

Layer 1 需要用到跟 Layer 2（`structured_filter_query_tool.resolve_arguments()`）同一份计数关键词核对逻辑——不能像本文档最初版本那样在两处各写一份 `_COUNTING_KEYWORDS`，那正是设计 A 要根治的"两套独立实现容易漂移"同一类问题在这里重演。提取成共享模块 `app/qa/counting_intent.py`：

```python
# app/qa/counting_intent.py
import re

_COUNTING_KEYWORDS = re.compile(r"多少|几个|数量|一共|总共|共有")


def drops_counting_keywords(original: str, rewritten: str) -> bool:
    """rewritten 相对 original 是否丢失了计数关键词——Layer 1（历史指代消解）
    和 Layer 2（structured_filter_query_tool 的 query_intent 保真核对）
    共用同一份判断，避免各自维护一份容易漂移的正则。"""
    if original == rewritten:
        return False
    original_kw = set(_COUNTING_KEYWORDS.findall(original))
    rewritten_kw = set(_COUNTING_KEYWORDS.findall(rewritten))
    return bool(original_kw - rewritten_kw)
```

`app/agent/tools/structured_filter_query/tool.py` 和 `app/qa/query_rewrite.py` 都从这里导入 `drops_counting_keywords()`，依赖方向是 `agent/tools` 依赖 `qa`（更通用、更底层的一侧被依赖），跟设计 A 里 `term_matcher` 依赖 `ontology_recall` 是同一个"谁更底层、被谁依赖"的原则。

```python
@dataclass(frozen=True)
class ResolvedQuestion:
    resolved_question: str
    duplicate_of: str | None  # 命中的历史轮次原文；没命中是 None，供设计 D 使用


_RESOLVE_QUESTION_SYSTEM_PROMPT = (
    "你是多轮对话的指代消解助手。给定最近几轮对话历史和用户当前这一句话，"
    "判断当前这句话是否依赖历史才能独立理解。\n"
    "默认认为不依赖——只有当前问题包含明确指代（它/这个/上面提到的）或"
    "存在脱离上下文无法执行的明显省略时，才判定为依赖历史。\n"
    "只输出 JSON：{\"depends_on_history\": true/false, "
    "\"resolved_question\": \"...\", \"duplicate_of\": \"...\"}\n"
    "resolved_question：depends_on_history=false 时必须逐字等于用户当前问题，"
    "不允许任何改写；=true 时只允许补全缺失的指代对象本身，不概括、不重写"
    "当前问题里已经出现的其余内容。\n"
    "duplicate_of：如果 resolved_question 在语义上跟历史里某一轮用户已经"
    "问过、且已经得到回答的问题基本相同，把那一轮的原始用户提问文本填在"
    "这里；没有这种情况就填空字符串。"
)


async def resolve_question(
    question: str,
    history: list[dict[str, str]],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 1.5,
) -> ResolvedQuestion:
    """历史指代消解，顺带检测当前问题是否在问一个最近已经问过并回答过的
    问题（供设计 D 使用，同一次调用产出，不新增 LLM 调用）。

    失败/超时/解析失败均回退"不依赖历史、原样返回、无重复"——这是这个
    函数"下限不比不做这一步差"的保证，跟 rewrite_query() 现有的失败处理
    原则一致。
    """
    messages = [
        {"role": "system", "content": _RESOLVE_QUESTION_SYSTEM_PROMPT},
        {"role": "user", "content": f"history: {history}\nquestion: {question}"},
    ]
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(messages=messages),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("resolve_question 超时，回退原始问题、无重复标记")
        return ResolvedQuestion(resolved_question=question, duplicate_of=None)
    except Exception:
        logger.warning("resolve_question 调用失败，回退原始问题、无重复标记", exc_info=True)
        return ResolvedQuestion(resolved_question=question, duplicate_of=None)
    try:
        payload = json.loads(result.text)
        resolved = str(payload.get("resolved_question") or "").strip() or question
        duplicate_of = str(payload.get("duplicate_of") or "").strip() or None
    except json.JSONDecodeError:
        logger.warning("resolve_question 返回内容不是合法 JSON，回退原始问题、无重复标记")
        return ResolvedQuestion(resolved_question=question, duplicate_of=None)

    # 跟 structured_filter_query_tool 的 is_verbatim 核对同一个原则：不信任
    # depends_on_history/resolved_question 的自我报告本身，只要改写后原文里
    # 的计数关键词丢了，就不采信这次改写，回退原文。
    if drops_counting_keywords(question, resolved):
        resolved = question
    return ResolvedQuestion(resolved_question=resolved, duplicate_of=duplicate_of)
```

图节点：

```python
async def resolve_question_node(state: AgentState) -> dict[str, Any]:
    history = state.get("memory_context_messages", [])
    if not history:
        # Phase 0 预检：没有可用历史，直接跳过 LLM 调用——跟 swiftagent
        # get_rewriting_decision_prompt_v2 的 Phase 0 同一个思路，也是这次
        # "零成本跳过"目标的落地。
        return {"resolved_question": state["question"]}
    result = await resolve_question(
        state["question"], history,
        llm_registry=llm_registry, llm_provider_name=llm_provider_name,
    )
    return {
        "resolved_question": result.resolved_question,
        "duplicate_of": result.duplicate_of,
    }
```

`graph.add_edge("memory_recall", "merge_after_parallel")` 改成 `graph.add_edge("memory_recall", "resolve_question")` + `graph.add_edge("resolve_question", "merge_after_parallel")`。

### `resolved_question` 的接入点：替换原始问题成为下游的"当前问题"

- `planner_node` 初始化 `planner_messages` 时，`messages.append({"role": "user", "content": state["question"]})` 改成 `state.get("resolved_question", state["question"])`（兜底：Layer 1 未跑过/失败时退回原始问题）。
- 各处构造 `ToolContext(question=state["question"], ...)` 的地方，改成 `question=state.get("resolved_question", state["question"])`——**设计 B 早前已经写好的 `is_verbatim` + 关键词核对逻辑不需要跟着改代码**，只是它比对的"ground truth"从原始问题自动变成了已消解指代的版本，覆盖面更完整。
- 确定性检索路径（非 Planner 模式）的 `_PROMPT_TEMPLATE.format(context=context, question=state["question"])` 同样改用 `resolved_question`——这条路径同样有 `memory_context_messages` 注入，指代消解对它同样有价值。

### Layer 2：轮内转述保真（沿用已设计的 `is_verbatim` + 关键词核对，不变）

`structured_filter_query_tool` 的 `query_intent`/`is_verbatim` schema 改动和 `resolve_arguments()` 里的确定性核对逻辑，见下方"Schema 改动"和"代码改动"——内容跟本文档更早版本一致，**唯一的变化是它比对的 `context.question` 现在已经是 Layer 1 消解后的问题**，不需要额外改动这部分代码本身。

Layer 2 继续按 Planner 每一轮工具反馈自适应调整（不受 Layer 1 影响）——这是有意保留的行为，不是遗留的 bug：第一次查不到、换个角度再查，属于合理的探索过程，不应该被"锁死成一次性改写"。

### Schema 改动：新增 `is_verbatim` 字段

`app/agent/tools/structured_filter_query/manifest.yaml`：

```yaml
parameters_schema:
  type: object
  properties:
    query_intent:
      type: string
      description: >
        默认原样填入用户当前问题的原文。只有当用户问题依赖前文指代
        （"它"/"这个"/"上面提到的"）或存在明显省略、脱离上下文无法独立
        执行时，才允许做最小改写——仅补全缺失的指代对象本身，不改写、
        不概括、不重新组织其余内容。补全后的句子必须完整保留用户当前
        问题里所有显式出现的措辞（尤其是"多少个/数量/一共/共有"这类
        计数用词、具体实体名、数值条件），禁止为了"更清楚"而概括或
        简化它们。如果当前问题本身已经完整、不依赖任何指代，直接原样
        返回，不要改写。
    is_verbatim:
      type: boolean
      description: >
        true 表示 query_intent 就是用户当前问题的原文，未做任何改写；
        false 表示做了指代补全式的最小改写。默认应该是 true——只有当前
        问题确实依赖前文指代、脱离上下文无法独立执行时，才允许 false。
  required:
    - query_intent
    - is_verbatim
```

### 代码改动：`resolve_arguments()` 里的确定性核对

`app/agent/tools/structured_filter_query/tool.py`：

```python
from app.qa.counting_intent import drops_counting_keywords

class StructuredFilterQueryTool:
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        query_intent = str(raw_arguments.get("query_intent") or "").strip() or context.question
        # 校验不看 is_verbatim 的值本身——它的价值在于强制模型对"有没有
        # 改写"做一次显式承诺（借的是 swiftagent 的核心机制），不代表这个
        # 自我报告就一定可信。只要 query_intent 跟原文不同、且原文里的计数
        # 关键词在改写后丢了，就不信任这次改写，无论模型自己说 is_verbatim
        # 是 true 还是 false。
        if drops_counting_keywords(context.question, query_intent):
            query_intent = context.question
        ...
```

**为什么不止步于"改进提示词描述"**：本次会话已经验证过两次纯提示词调整（改字段描述、改 `trigger_cue`）都不能稳定改变这个模型的行为。`is_verbatim` 字段本身是从 swiftagent 的 `rl` 分级机制借来的核心机制（不是措辞技巧）——强制模型对"有没有改写"做一次显式的、可校验的布尔承诺，而不是让这个判断隐藏在自由文本生成的内部过程里。但即便如此，仍然不能完全信任模型自我报告的准确性（`is_verbatim` 本身也可能被错误标注），所以额外加一层不依赖 LLM 判断力的确定性核对——这是防御纵深，不是相信某一层就够了。

**为什么核对只在文本不同时触发，不是无条件跑**：`drops_counting_keywords()` 内部先判断 `original == rewritten`，逐字相等时直接返回 `False`——不存在"改写后关键词丢失"这件事，核对逻辑本身没有意义，跳过是纯粹的性能优化，不影响正确性。

**范围限制**：`counting_intent.py` 里的关键词集合只覆盖"计数意图丢失"这一类已经确认造成过真实 bug 的关键词，不打算做成覆盖所有可能语义槽位的通用校验框架（YAGNI）——如果未来发现其他类别的信息在改写中丢失且造成了具体故障，再单独评估是否要扩展这个关键词集合或校验维度。

### `_USAGE_GUIDE` 联动调整

`is_verbatim` 字段本身在 `manifest.yaml` 里已经说明了语义，不需要在 `_USAGE_GUIDE`（深层参数生成看到的详细说明）里重复展开——`_USAGE_GUIDE` 面向的是"拿到 query_intent 之后该怎么选 anchor 模式"，跟"query_intent 是怎么产生的"是两个不同阶段的关注点，不应该混在一起。

### 两处范围决策

1. **`is_verbatim=true` 但 `query_intent != context.question` 时，同样强制覆盖。** 理论上 `is_verbatim=true` 应该意味着两者逐字相等；不等就说明模型的自我报告本身不可靠（说了没改写，实际改了）。`drops_counting_keywords()` 不看 `is_verbatim` 的值，只看文本本身——避免实现里出现两条并行、容易漂移的校验路径。`is_verbatim` 字段的价值仍然在于"强制模型做一次显式承诺"这个动作本身（借的是 swiftagent 的核心机制），不在于校验逻辑要不要读它的值。Layer 1 的 `resolve_question()` 同理不看 `depends_on_history` 的值。
2. **关键词集合只覆盖计数关键词，不覆盖实体名等其他槽位。** 本次会话观察到的具体 bug 只涉及计数关键词丢失，没有直接证据表明实体名会在改写中被丢弃（Planner 转述时基本都保留了实体名，只是把动作/意图部分概括掉了）。当前设计范围只覆盖已验证的失败模式，不做推测性覆盖——YAGNI。

## 设计 C：`app/qa/query_rewrite.py::rewrite_query()` 职责收窄——只做检索友好化

Layer 1 已经统一解决了指代消解，`rewrite_query()` 不再需要自己承担这部分职责——它现状的 `_SYSTEM_PROMPT` 混了两件事（"结合历史补全指代"+"改写成更利于检索的规范术语表达"），现在收窄成只做后者。

`vector_search_tool.execute()` 里 `hybrid_search()` 拿到的 `query` 参数来自 Planner 当轮生成的 `arguments.get("query")`——Planner 生成这个参数时，它的 `planner_messages` 已经是基于 `resolved_question`（Layer 1 消解后）构造的，所以这个 `query` 参数本身天然更可能已经是指代清晰的。`rewrite_query()` 因此不再需要 `conversation_context` 参数来做指代消解——**这也解决了本次会话中途发现的具体缺口**（`hybrid_search()` 早就支持 `conversation_context` 参数，但 `vector_search_tool.execute()` 调用时从未传过）：这个缺口现在不需要用"补上没传的参数"来修，因为指代消解已经统一在更上游（Layer 1）解决了，不需要 `rewrite_query()` 自己再单独具备这个能力。`conversation_context` 参数保留在函数签名里（向后兼容，`answer.py` 里非 Planner 的确定性路径可能仍需要），但 Planner/Agent 路径不再需要传它。

```python
_SYSTEM_PROMPT = (
    "你是客服问答检索的 query 改写助手。"
    "如果用户的问题已经清晰、具体，直接原样返回，不要改写。"
    "只有当问题用词过于口语化、不利于文档检索匹配时，才改写成更规范的"
    "术语表达。"
    "改写后的句子必须保留原始问题里所有具体词语和限定条件，"
    "不能为了检索友好而丢弃或概括它们。"
    "只输出改写后的一句话，不要解释。"
)
```

### 测试

- `tests/qa/test_query_rewrite.py`（如果存在，否则新建）补一条用例：原始问题已经清晰具体时，改写结果应该逐字等于原始问题。

## 设计 D：基于 Layer 1 顺带产出的重复问题软提示

### 动机

`resolved_question` 是一个稳定、精确的文本——如果本轮问题（消解指代后）在语义上跟最近历史里某一轮已经问过、已经得到回答的问题基本相同，理论上可以直接复用那个答案，不需要重新触发一次工具调用。这个能力在改写统一之前不好做（Planner 每轮临时生成的措辞不稳定，难以可靠地跟历史比对），改写统一之后，`resolved_question` 天然具备了做这件事的条件。

**这不会引入新的重复调用风险，也不解决现有的重复调用问题**——同一轮内部 Planner 自己反复查询同一件事（本次会话已实测过的"查两次都对、第三次自己引入错误又推翻前两次"）是 Layer 2 自适应探索的固有行为，跟 Layer 1 无关，Layer 1 只在一轮的最开始跑一次。跨轮次的重复提问，在当前系统里本来就没有任何检测/复用机制——这个缺口不是本设计造成的，只是本设计恰好提供了低成本填补它的条件。

### 设计：软提示，不做硬性自动短路

`resolve_question()`（设计 B）已经在同一次 LLM 调用里顺带产出 `duplicate_of`（命中的历史轮次原文，未命中为空）——不新增 LLM 调用。

**不自动跳过工具调用**——一次误判的"重复检测"如果直接短路，用户会收到一个可能过时/不准确的答案且系统不会重新核实，风险不可控。改成软提示：命中时，往 `planner_messages` 里插入一条 system 消息，让 Planner 自己判断要不要复用：

```python
async def planner_node(state: AgentState) -> dict[str, Any]:
    messages = state.get("planner_messages")
    if not messages:
        messages = [...]  # 省略号代表设计 B 已经改好的现有初始化逻辑
                           # （system prompt/term_guard_context/memory_context_messages/
                           # 用 resolved_question 构造的 user 消息），这里只展示新增部分
        if duplicate_of := state.get("duplicate_of"):
            messages.append({
                "role": "system",
                "content": (
                    f"提示：当前问题跟历史里已经问过的『{duplicate_of}』可能是"
                    "同一个问题。如果确实相同、历史对话里已经给出过明确答案，"
                    "可以直接复用那个答案，不需要重新调用工具查询；如果不确定"
                    "是否完全相同，仍然应该重新查询确认，不要仅凭这条提示就"
                    "给出可能过时或不准确的答案。"
                ),
            })
        ...
```

是否要复用、要不要重新核实，决定权留给 Planner——这条提示只是把"这可能是重复问题"这个信息摆到它面前，不替它做决定。

### 测试

- `tests/qa/test_query_rewrite.py` 新增：`resolve_question()` 命中重复问题时正确产出 `duplicate_of`；未命中时为空；LLM 调用失败/超时时 `duplicate_of` 为空（不阻塞主链路）。
- `tests/agent/test_graph.py`（或等价文件）新增：`state["duplicate_of"]` 有值时，`planner_messages` 里包含对应的提示 system 消息；无值时不包含。

## 测试策略（汇总）

- 设计 A：`tests/graphrag/test_term_matcher.py` 全量回归 + 新增"两处匹配结论一致"用例。
- 设计 B：
  - `tests/qa/test_counting_intent.py`（新建）：`drops_counting_keywords()` 的用例——文本相同时返回 `False`；改写后关键词丢失时返回 `True`；改写后关键词保留（哪怕措辞变了）时返回 `False`。这是设计 B/D 共用的基础，独立测，不用在两处调用方测试里重复覆盖这些边界情况。
  - `tests/qa/test_query_rewrite.py` 新增 `resolve_question()` 的用例：无历史时零成本跳过（不发起 LLM 调用）；有历史且不依赖时 `resolved_question` 逐字等于原文；依赖历史时正确补全指代；LLM 失败/超时/解析失败时回退原文；模型改写后计数关键词丢失时被 `drops_counting_keywords()` 拦截、回退原文。
  - `tests/agent/test_graph.py`（或等价文件）新增：`resolve_question_node` 正确写入 `state["resolved_question"]`；`planner_node`/`ToolContext` 构造正确使用 `resolved_question` 而非原始 `question`。
  - `tests/agent/tools/test_structured_filter_query.py` 现有的 `is_verbatim`/关键词核对用例改为断言调用了 `drops_counting_keywords()`（或等价的黑盒行为断言），不再直接依赖 `tool.py` 里自己的正则实现。
- 设计 C：`tests/qa/test_query_rewrite.py` 新增/补全"原始问题已清晰，不改写"用例；确认 `conversation_context` 参数不再从 `vector_search_tool.execute()` 传入。
- 设计 D：见上方"设计 D"小节的测试。
- 全部改动后跑一次全量测试套件确认无回归。

## 范围外

- `resolve_term()`（`anchor.name` 模式下的精确匹配）不在本次设计范围内——它服务的目的（"commit 到唯一节点前的最后一道精确校验"）跟本文档讨论的问题都不同，本次会话已经确认它的策略（精确匹配、不模糊）是合理的，不需要跟着改。
- ETL 配置（`产品→公司` 关系建模方式，多对多语义）不在本次设计范围内——这是数据源/建模层面的独立问题，本次会话已经分析过，不属于查询匹配/改写逻辑的范畴。
- 设计 D 的"重复问题检测"只做软提示，不做自动答案复用/自动短路工具调用——如果后续观察到 Planner 对软提示的采纳率不理想（比如总是选择重新查询，提示形同虚设），是否要升级成更强的机制（比如更高置信度时的自动短路），留作后续单独评估，本次不做。
