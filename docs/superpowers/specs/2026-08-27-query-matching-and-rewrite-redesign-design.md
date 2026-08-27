# 查询匹配统一 + query_intent 改写重设计

## 背景

2026-08-27 深挖"coke-cola公司有多少个订单"这条链路时，确认了两处独立的、真实发生过 bug 的结构性问题——不是措辞调整能根治的，需要重新设计数据流。

### 问题 1：两套互不通信的实体匹配实现

系统里有两处独立代码，各自回答"这段文本提到了哪个已知实体"这个同一个问题：

- `app/graphrag/term_guard.py::match_terms()`——在 Planner 推理**之前**无条件跑，用 `difflib.SequenceMatcher` 逐候选滑窗打分（阈值 0.75），命中的术语的一跳邻居会被强制注入系统上下文。
- `app/graphrag/ontology_recall.py::recall_ontology_candidates()`——只在 `structured_filter_query_tool.resolve_arguments()` 内跑，用 `longest_common_substring_score()`（最长公共连续子串长度 / 候选长度，带 `_MIN_OVERLAP_LENGTH` 下限）+ n-gram 预过滤打分排名，结果当作候选参考喂给深层参数生成 LLM。

两套算法对长短候选词的打分特性不同：`SequenceMatcher` 对长候选词的局部差异更敏感（容易把分数拉低），对短候选词的巧合重叠更宽容。这直接导致了这次的真实 bug——用户输入"coke-cola"时，`match_terms()` 用 `SequenceMatcher` 算出"Coca-Cola"（9字符，公司）相似度约 0.55（不过阈值），"Cola"（4字符，产品）相似度恰好压线 0.75（过阈值）——于是 `term_guard` 只注入了错误实体"Cola"产品的上下文（本例中还带出了 996 条无关订单号），把 Planner 从第一个 token 起就带偏，尽管 `recall_ontology_candidates()` 后来是能正确同时召回两个实体的（已通过本次会话早前的另一处修复——大小写不敏感——验证）。

两处算法不统一，意味着同一段文本在两处会得出不一致、甚至矛盾的匹配结论，且系统里没有任何一处会发现/警告这种矛盾。

已排除的合并方向：让两处共享同一次匹配计算（“算一次、都复用”）。这条路径已确认不可行——`recall_ontology_candidates()` 的完整输入是 `f"{query_intent}\n{context.question}"`，而 `query_intent` 要等 Planner 推理完成、生成 tool_calls 参数之后才存在，这个时间点严格晚于 `term_guard_node`（在 Planner 推理之前跑）已经执行完毕的时刻——`term_guard` 该做“共享计算”的那一刻，`recall_ontology_candidates()` 的完整输入根本还不存在。折中的“缓存 term_guard 对 `context.question` 的匹配结果、`recall` 复用后再对 `query_intent` 增量扫描”方案，只能省掉重复扫描 `context.question` 这一次开销（`query_intent` 部分仍要单独扫），收益小、复杂度增（需要合并两次结果），不予采用。

### 问题 2：`query_intent` 的自由转述是"计数意图丢失"系列 bug 的结构性根源

`structured_filter_query_tool` 的参数生成拆成两次独立 LLM 调用（渐进式披露设计，见 `2026-08-25-progressive-disclosure-recall-augmented-params-design.md`）：Planner（环节 2）生成一段自由文本 `query_intent`，深层参数生成 LLM（环节 3）只看到这段转述文本（加上本次会话早前作为兜底加入的 `context.question` 原文）来决定 `anchor`/`constraints`。

`query_intent` 当前的 schema 描述鼓励"用自然语言描述这次想查询的内容"——这是一个默认执行"概括/转述"的开放式指令，没有"默认不改写"这个基线，也没有"当前问题里已经出现的措辞必须原样保留"这条约束。本次会话实测反复观察到：Planner 把用户"coke-cola公司有多少个订单"这个明确的计数问题，转述成"查询产品 Cola 的信息"、"查询 Coca-Cola 这个产品的信息"这类丢失了"多少个/订单"关键信息的版本，导致下游深层参数生成 LLM 选择了错误的 `anchor.name` 模式而不是 `anchor.term_type + constraints` 计数模式。

本次会话已经尝试过两次纯提示词层面的补救（改 `query_intent` 字段描述、改 `trigger_cue`），均未能稳定生效——问题不在于文案写得够不够清楚，而在于当前机制完全依赖 LLM 的注意力/遵从度，没有任何结构性约束或校验。

参考本地项目 `swiftagent`（`dev/2.7.5` 分支，`memory/rewriting_prompt.py::get_rewriting_decision_prompt_v2`）成熟的查询改写决策设计，其可靠性不只来自提示词措辞，更来自**强制模型输出一个结构化的、可校验的改写决策**（`r`/`rl`/`inherit` 等字段），而不是让"要不要改写、改了什么"隐藏在一段自由文本生成的内部过程里。

顺带确认：`app/qa/query_rewrite.py::rewrite_query()`（服务于 `vector_search_tool` 检索的独立改写模块）存在同样的设计缺陷——`_SYSTEM_PROMPT` 同样是完全开放式的"改写为更利于检索的表达"，没有"默认不改写"、没有"显式槽位保留"约束。本次会话未观察到它引发过具体故障案例，但按同样原则一并修正，避免同类问题日后在这条路径上复现。

## 目标

- 统一两处实体匹配算法，消除"同一段文本两处给出矛盾结论"的可能性。
- 让 `query_intent` 的生成从"默认转述"变成"默认原样，仅在必要时最小改写"，并为"是否改写"这个判断提供一层不依赖 LLM 自我报告的确定性核对。
- 用同样的"默认不改写 + 显式槽位保留"原则修正 `rewrite_query()`，不引入额外的结构化校验机制（检索场景容错度高，不值得加重）。
- 不引入新的基础设施依赖，不新增独立的 LLM 调用（`is_verbatim` 作为 Planner 已有的 function-calling 参数的搭配字段，不是新的调用）。

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

## 设计 B：`query_intent` 改写重设计

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
import re

# 跟 term_guard/ontology_recall 的模糊匹配是两回事——这里不判断"是不是在问
# 某个实体"，只做最基础的"这几个计数关键词有没有在改写后的文本里消失"检测，
# 纯规则、不依赖 LLM 判断力，作为 is_verbatim 自我报告失真时的兜底。
_COUNTING_KEYWORDS = re.compile(r"多少|几个|数量|一共|总共|共有")


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
        if query_intent != context.question:
            question_kw = set(_COUNTING_KEYWORDS.findall(context.question))
            intent_kw = set(_COUNTING_KEYWORDS.findall(query_intent))
            if question_kw - intent_kw:
                query_intent = context.question
        ...
```

**为什么不止步于"改进提示词描述"**：本次会话已经验证过两次纯提示词调整（改字段描述、改 `trigger_cue`）都不能稳定改变这个模型的行为。`is_verbatim` 字段本身是从 swiftagent 的 `rl` 分级机制借来的核心机制（不是措辞技巧）——强制模型对"有没有改写"做一次显式的、可校验的布尔承诺，而不是让这个判断隐藏在自由文本生成的内部过程里。但即便如此，仍然不能完全信任模型自我报告的准确性（`is_verbatim` 本身也可能被错误标注），所以额外加一层不依赖 LLM 判断力的确定性核对——这是防御纵深，不是相信某一层就够了。

**为什么核对只在 `query_intent != context.question` 时触发，不是无条件跑**：两者逐字相等时，不存在"改写后关键词丢失"这件事，核对逻辑本身没有意义，跳过是纯粹的性能优化，不影响正确性。

**范围限制**：`_COUNTING_KEYWORDS` 只覆盖"计数意图丢失"这一类已经确认造成过真实 bug 的关键词，不打算做成覆盖所有可能语义槽位的通用校验框架（YAGNI）——如果未来发现其他类别的信息在改写中丢失且造成了具体故障，再单独评估是否要扩展这个关键词集合或校验维度。

### `_USAGE_GUIDE` 联动调整

`is_verbatim` 字段本身在 `manifest.yaml` 里已经说明了语义，不需要在 `_USAGE_GUIDE`（深层参数生成看到的详细说明）里重复展开——`_USAGE_GUIDE` 面向的是"拿到 query_intent 之后该怎么选 anchor 模式"，跟"query_intent 是怎么产生的"是两个不同阶段的关注点，不应该混在一起。

### 两处范围决策

1. **`is_verbatim=true` 但 `query_intent != context.question` 时，同样强制覆盖。** 理论上 `is_verbatim=true` 应该意味着两者逐字相等；不等就说明模型的自我报告本身不可靠（说了没改写，实际改了）。采用统一、简单的规则：只要 `query_intent != context.question` 且原文本里检测到的计数关键词集合不是 `query_intent` 关键词集合的子集，就用 `context.question` 覆盖——不区分 `is_verbatim` 是 `true` 还是 `false`，避免实现里出现两条并行、容易漂移的校验路径。`is_verbatim` 字段的价值仍然在于"强制模型做一次显式承诺"这个动作本身（借的是 swiftagent 的核心机制），不在于校验逻辑要不要读它的值。
2. **`_COUNTING_KEYWORDS` 只覆盖计数关键词，不覆盖实体名等其他槽位。** 本次会话观察到的具体 bug 只涉及计数关键词丢失，没有直接证据表明实体名会在改写中被丢弃（Planner 转述时基本都保留了实体名，只是把动作/意图部分概括掉了）。当前设计范围只覆盖已验证的失败模式，不做推测性覆盖——YAGNI。

## 设计 C：`app/qa/query_rewrite.py::rewrite_query()` 轻量优化

只改 `_SYSTEM_PROMPT`，不引入结构化字段/确定性核对（检索场景容错度高，改写走样的代价是检索质量略降，不是确定性错误答案，不值得加重）：

```python
_SYSTEM_PROMPT = (
    "你是客服问答检索的 query 改写助手。"
    "如果用户的问题已经清晰、不依赖对话历史就能独立理解，直接原样返回，不要改写。"
    "只有当问题包含模糊指代（比如“这个报错”指代的具体错误码/模块）时，"
    "才结合此前的对话历史补全该指代，尽量使用规范术语。"
    "改写后的句子必须保留原始问题里所有具体词语和限定条件，"
    "不能为了检索友好而丢弃或概括它们。"
    "只输出改写后的一句话，不要解释。"
)
```

### 测试

- `tests/qa/test_query_rewrite.py`（如果存在，否则新建）补一条用例：原始问题已经完整、不含指代时，改写结果应该逐字等于原始问题（当前测试大概率没有覆盖"不改写"这个分支，需要确认现状后再决定新增还是补全）。

## 测试策略（汇总）

- 设计 A：`tests/graphrag/test_term_matcher.py` 全量回归 + 新增"两处匹配结论一致"用例。
- 设计 B：`tests/agent/tools/test_structured_filter_query.py` 新增 `is_verbatim=false` 且关键词丢失时触发核对回退的用例；`is_verbatim=true`/正常改写场景不触发。
- 设计 C：`tests/qa/test_query_rewrite.py` 新增/补全"原始问题已完整，不改写"用例。
- 全部改动后跑一次全量测试套件确认无回归。

## 范围外

- `resolve_term()`（`anchor.name` 模式下的精确匹配）不在本次设计范围内——它服务的目的（"commit 到唯一节点前的最后一道精确校验"）跟本文档讨论的两个问题都不同，本次会话已经确认它的策略（精确匹配、不模糊）是合理的，不需要跟着改。
- ETL 配置（`产品→公司` 关系建模方式，多对多语义）不在本次设计范围内——这是数据源/建模层面的独立问题，本次会话已经分析过，不属于查询匹配/改写逻辑的范畴。
