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
- 为"是否改写、缺了哪个槽位"引入强制显式决策字段（`is_verbatim`/`rl`+`inherited_slots`），提高模型对这个判断的注意力——这是提示词层面的手段，不是确定性校验（见"2026-08-28 决策变更"）。
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

### 2026-08-28 方案重定：原"统一到 longest_common_substring_score"被实测推翻

本文档最初版本主张把 `term_matcher._has_fuzzy_match()` 的 `difflib.SequenceMatcher` 换成
`ontology_recall.longest_common_substring_score()`。转实现计划前做实测，**这个方案被证伪，不能实施**：

| 用例 | LCS(最长连续子串) | SequenceMatcher |
|---|---|---|
| 错1字（"服务器**链**接超时"）**应命中** | 0.4286 | 0.8571 |
| 错2字（"服务器**网络**超时"）**不应命中** | 0.4286 | 0.7143 |
| coke-cola → **Coca-Cola**（正确·公司） | 0.5556 | 0.7778 |
| coke-cola → Cola（错误·产品） | 1.0000 | 1.0000 |

两个硬伤：

1. **LCS 数学上无法区分"该命中"和"不该命中"**——两者得分完全相同（都 0.4286），不存在任何阈值能同时满足这两条现有测试。原因是 LCS 只保留**单个最长连续片段**（两例都是`服务器`=3），后面的`接超时`因为被错字隔断成另一段，直接丢弃；SequenceMatcher 计的是**总匹配字符数**（6 vs 5），所以能区分。
2. **会让 coke-cola bug 复发**——`Coca-Cola` 从 0.7778（过 0.75 阈值）掉到 0.5556（不过），于是只有错误的 `Cola` 命中，正是提交 `d3336f0` 修好的那个原始 bug。实测确认当前实现返回 `['Coca-Cola', 'Cola']`，换 LCS 后只剩 `['Cola']`。

真正修好 coke-cola 的是**大小写不敏感**那个改动（已提交），不是算法选择。

### 重定方案：借鉴 swiftagent 2.7.5 召回算法的融合设计

参考 `swiftagent`（`dev/2.7.5`）`plugin/utils/get_common_str.py::get_common_str_and_score()`，
其召回阶段字符匹配有四层机制：token 化 → **带 `interval=3` 间隔约束**的回溯最长顺序子序列
（`_get_max_sequence`）→ **F-beta 双向评分**（`P=匹配/len(short)`、`R=匹配/len(query)`，β=0.2）
→ 另一路 Damerau-Levenshtein 分数做一致性融合。

对照本项目实测，确定三个融合点：

**融合点1：`interval` 间隔约束**——纯最长公共子序列（LCSubseq）有致命误匹配问题：
"服务里有个器件，连着接口，超过时限了" 对 "服务器连接超时" 得 **0.9430**（7个字散落各处全被匹配上）。
加 interval 约束后：`interval=3`（swiftagent 默认）仍是 0.9430 太松；**`interval=2` 压到 0.2694**；
`interval=1` 虽然也是 0.2694 但误伤正确用例（Coca-Cola 崩到 0.2149）。**取 `interval=2`**。

**融合点2：F-beta 双向评分**——解决本项目的核心痛点"短候选名虚高"。`Cola`(4字) 是 `Coca-Cola`(9字)
的子串，任何单向 `匹配/len(name)` 公式 Cola 都拿满分碾压：

| β | Coca-Cola（正确） | Cola（错误） | 结果 |
|---|---|---|---|
| 0.2（swiftagent 默认） | 0.7521 | 0.8889 | ✗ 短名字仍赢 |
| **0.5（本项目取值）** | **0.6604** | 0.6061 | ✓ 扭转 |
| 1.0 | 0.5385 | 0.3810 | ✓ 差距更大 |

swiftagent 用 β=0.2 是因为其指标召回场景更怕漏召；本项目这个痛点需要更高的 β，取 **β=0.5**。

**融合点3（结构性）：两侧不能共用同一个分数公式，只共用"匹配长度"计算**——
关键发现：`term_matcher` 的滑动窗口长度**恰好等于候选名长度**，因此 `P` 恒等于 `R`，
F-beta 数学上退化成 `P`，**β 在这一侧完全不起作用**。所以：

- **`term_matcher` 侧**：滑动窗口已天然保证局部性且 P=R → 只需 `interval + LCSubseq`，不引入 β
- **`ontology_recall` 侧**：全 query 远长于 name，R<P → β 才是解决短名虚高的关键

共享的是**底层匹配长度计算**，不是最终分数公式。

### 改动

**新增共享底层函数**，放在 `app/graphrag/term_matcher.py`（`ontology_recall.py` 反向依赖它，
方向与现状相反——`term_matcher.py` 不 import `ontology_recall.py`，避免循环依赖）：

```python
# app/graphrag/term_matcher.py
_DEFAULT_INTERVAL = 2


def matched_length(a: str, b: str, *, interval: int = _DEFAULT_INTERVAL) -> int:
    """b 的字符按顺序在 a 里最多能匹配上多少个，且相邻两个匹配字符在 a 中
    跨越的字符数不超过 interval。大小写不敏感。

    这是"最长公共子序列"加了间隔约束的版本——不加约束时，长文本里散落
    各处的字符也能被全部匹配上（实测："服务里有个器件，连着接口，超过时限了"
    对"服务器连接超时"得 0.9430），interval 把匹配限制在局部连贯的区域内。
    间隔取 2 是实测甜点：3 太松压不掉上面那个误匹配，1 太紧会误伤
    "coke-cola"→"Coca-Cola" 这类正常变体。机制借鉴 swiftagent dev/2.7.5
    的 get_common_str.py::_get_max_sequence（那里是 token 级 interval=3，
    这里按字符级、取 2）。

    dp[i][j] = 「x[i-1] 与 y[j-1] 配对、且这是最后一个匹配」时的最大匹配数。
    "最后一个匹配落在哪"必须编进状态本身。2026-08-28 实现时踩过的坑：写成
    dp[i][j]=前i前j的最大匹配数、另用一张 last[i][j] 记位置，不是合法的 DP
    ——在某个 (i,j) 上贪心取最大长度可能留下对后续间隔检查更不利的 last，
    整体反而更差。反例 matched_length("bbbbbbba", "baba", interval=2) 在那种
    写法下返回 2，真实最优是 3（brute-force 交叉验证）。
    """
    x, y = a.lower(), b.lower()
    if not x or not y:
        return 0
    n, m = len(x), len(y)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    # prefix_best[i][j] = max(dp[i][1..j])，用于 O(1) 查"上一个匹配落在 x 的
    # 第 i 个字符、只用掉 y 的前 j 个字符"这族状态的最好成绩。
    prefix_best = [[0] * (m + 1) for _ in range(n + 1)]
    answer = 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if x[i - 1] == y[j - 1]:
                # 间隔约束 (i-1)-(i2-1)-1 <= interval 化简成
                # i2 >= i-interval-1，往回只看 interval+1 个位置。
                prev = 0
                for i2 in range(max(1, i - interval - 1), i):
                    if prefix_best[i2][j - 1] > prev:
                        prev = prefix_best[i2][j - 1]
                dp[i][j] = prev + 1
                if dp[i][j] > answer:
                    answer = dp[i][j]
            prefix_best[i][j] = max(prefix_best[i][j - 1], dp[i][j])
    return answer
```

**`term_matcher._has_fuzzy_match()` 改用它**（阈值 0.75 保持不变）：

```python
def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
    window = len(candidate)
    if window == 0 or len(text) < window:
        return False
    for i in range(len(text) - window + 1):
        # 窗口长度 == 候选名长度，所以这里的 P 恒等于 R，不需要 F-beta，
        # 直接用匹配长度占候选名长度的比例即可。
        if matched_length(text[i : i + window], candidate) / window >= threshold:
            return True
    return False
```

`match_terms()` 的精确匹配分支（`candidate.lower() in text_lower`）完全不动。

**`ontology_recall` 侧改用 F-beta**——`_ngram_score_prelowered`/`longest_common_substring_score`
的 LCS 打分换成 `matched_length` + F-beta：

```python
# app/graphrag/ontology_recall.py
from app.graphrag.term_matcher import matched_length

_FBETA = 0.5  # <1 偏向精确率P，但要大到足以让 R 惩罚"短候选名虚高"


def fbeta_match_score(query: str, name: str, *, beta: float = _FBETA) -> float:
    """query 与候选名 name 的匹配分。P=匹配长度/len(name)（匹配了候选名多少
    内容），R=匹配长度/len(query)（匹配占 query 多大比例）。

    必须用双向的 F-beta 而不是单向的 P：`Cola`(4字) 是 `Coca-Cola`(9字) 的
    子串，只看 P 的话 Cola 永远拿满分 1.0 碾压 Coca-Cola——这正是
    2026-08-27 "coke-cola公司有多少个订单" 召回到错误实体的成因之一。
    R 项让"query 很长却只匹配上一小段"的短候选名掉分。β=0.5 是实测取值
    （β=0.2 时 Cola 0.8889 仍高于 Coca-Cola 0.7521；β=0.5 时 Coca-Cola
    0.6604 反超 Cola 0.6061）。
    """
    if not name or not query:
        return 0.0
    if name.lower() in query.lower():
        matched = len(name)          # 整段包含：满额匹配，保留现有快速路径语义
    else:
        matched = matched_length(query, name)
    if matched == 0:
        return 0.0
    precision = matched / len(name)
    recall = matched / len(query)
    b2 = beta * beta
    return (1 + b2) * precision * recall / (b2 * precision + recall)
```

`_best_score()` 内的 n-gram 循环 + bigram 预过滤**保留**（性能优化，实测 5000 候选
110.1ms vs 现状 107.3ms 基本持平），只把其中的打分函数换掉。

**F-beta 只用于实体召回，不用于本体词汇召回**（2026-08-28 实现时发现并修正）：
`_best_score()` 服务四类候选，但它们的统计特性不同——

| 候选类型 | 例子 | 名字短意味着 |
|---|---|---|
| `term_types` | 订单号、公司、产品 | **正常**：本体标签天生就短 |
| `relations` | 上述标签组成的三元组 | **正常** |
| `fields` | 字段名 | **正常** |
| `entities` | Coca-Cola、Cola | **就是 bug**：短名字盖过正确的长名字 |

F-beta 的 recall 项（`匹配/len(query)`）专治"短候选名只覆盖长 query 的一小片却拿高分"，
这对实体名是对的，对本体词汇是错的——后者无论 query 多长都该是短的。实测 query
`"查询Coca-Cola这家公司名下有多少个订单"`：F-beta 给"订单号" 0.2857（低于 `_MIN_SCORE`
0.3，被错误丢弃）、"用户名" 0.1429；precision-only 给 0.6667 / 0.3333。反过来在实体侧
F-beta 不可替代——precision-only 下 `Cola` 得 1.0 压过 `Coca-Cola` 的 0.7778，正是要修的
那个 bug。

所以另设一个 `precision_match_score(query, name)`（只算 `匹配长度/len(name)`，单向），
通过 `_best_score(..., score_fn=...)` 参数切换：`term_types`/`relations`/`fields` 用
默认的 `precision_match_score`，只有 `entities` 传 `score_fn=fbeta_match_score`。
`_MIN_SCORE`（0.3）和 `_FBETA`（0.5）都不改。

### 已实测验证的结果

- `term_matcher` 侧：`tests/graphrag/test_term_matcher.py` **现有 12 条测试全部通过**，
  阈值 0.75 无需调整，`test_match_terms_known_false_positive_for_similar_short_error_codes`
  记录的 E502/E503 已知误报行为也保持不变（新算法下同样是 0.75 压线）。
- `ontology_recall` 侧：β=0.5 时 `Coca-Cola`(0.6604) 正确排在 `Cola`(0.6061) 之前。

### 测试

- `tests/graphrag/test_term_matcher.py` 全量回归，确认 12 条现有用例行为不变。
- 新增 `matched_length()` 的直接单元测试：无间隔超限时等于普通最长公共子序列长度；
  间隔超限时被截断（用"服务里有个器件，连着接口，超过时限了" vs "服务器连接超时"，
  断言 `interval=2` 的结果显著小于 `interval=None` 的等价值）；大小写不敏感；空串返回 0。
- 新增 `fbeta_match_score()` 单元测试：断言 `fbeta_match_score(q, "Coca-Cola") >
  fbeta_match_score(q, "Cola")`（q="coke-cola公司有多少个订单"）——直接钉住"短候选名
  虚高"这个痛点被修复。
- 新增跨模块一致性测试：同一段文本 "coke-cola公司有多少个订单" 分别过 `match_terms()`
  和 `recall_ontology_candidates()`，断言两者都能召回 `Coca-Cola`（不再一个命中一个不命中）。

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

> **2026-08-28 决策变更**：本文档更早版本在这里设计了一个共享的确定性核对模块 `app/qa/counting_intent.py::drops_counting_keywords()`（正则匹配计数关键词，改写后丢失就不采信、回退原文），Layer 1/Layer 2 都用它做"不依赖 LLM 自我报告"的事后校验。复审时确认这个关键词表本身覆盖面太窄（只认"多少/几个/数量/一共/总共/共有"六个词，"有几家""合计多少件"这类同样是计数意图的表达会漏检），且"用更泛化的方式做这件事"（语义相似度阈值、LLM 自评、意图分类器）无一例外都会重新引入某种软判断，跟这道核对存在的意义（不依赖软判断）自相矛盾。讨论后**明确决定去掉这层确定性核对，完全依赖提示词本身**（`is_verbatim`/`depends_on_history` 这类强制显式决策字段 + 精心设计的 schema 描述）。这是一个已知有风险的取舍——本次会话已经验证过两次纯提示词调整对这个模型不够可靠，去掉核对后不再有任何非 LLM 的最后防线；如果上线后复测发现"计数意图丢失"这类问题仍然复现，下一步的候选方向是重新加回某种核对（不一定是关键词匹配），而不是回到反复调整提示词措辞这条已经验证过两次无效的路径。

### 槽位填充：借鉴 swiftagent `get_rewriting_decision_prompt_v2` 的核心机制，不照搬 i/e/t/d

`depends_on_history: true/false` 这种粗粒度二元判断，只能回答"要不要改写"，回答不了"缺的具体是什么、该从历史补哪一部分"——跟 swiftagent 槽位填充机制的核心差距正在这里。customer_rag 场景下的槽位不能照搬 swiftagent 的 i(指标)/e(主体)/t(时间)/d(维度)（那是数据分析场景的划分），改成贴合 `structured_filter_query_tool` 查询结构的三类：

- **`anchor`**——问题在问哪个具体实体或实体类型（"Coca-Cola"、"订单号"）。
- **`intent_type`**——问题的意图类型（计数/列举/查详情/比较）——直接对应本次会话反复追的"计数意图丢失"这个具体 bug，是最关键的一类槽位。
- **`constraint`**——问题里的过滤/限定条件（"属于 XX 公司"、"大于 500"）。

**不包含** `relation_path`（多跳关系路径具体怎么走）——那是 Layer 3（`resolve_arguments`）的技术细节，通过 `recall_ontology_candidates`/`_USAGE_GUIDE` 处理，Layer 1 只负责"这句话本身讲没讲清楚"，下沉到关系路径会跟 Layer 3 职责重叠。也不引入 swiftagent 的"时间"槽位/`rl=2`弱改写档——customer_rag 目前没有观察到显著的时间范围查询场景，只保留 `rl=3`（不改写）和 `rl=1`（强改写，补全 `anchor`/`intent_type`/`constraint` 中任意缺失的子集）两档，不为了看起来完整而引入用不上的第三档。

```python
@dataclass(frozen=True)
class ResolvedQuestion:
    resolved_question: str
    inherited_slots: list[str]  # 实际从历史继承的槽位，anchor/intent_type/constraint 的子集；不依赖历史时为 []
    duplicate_of: str | None  # 命中的历史轮次原文；没命中是 None，供设计 D 使用


_RESOLVE_QUESTION_SYSTEM_PROMPT = (
    "你是多轮对话的指代消解助手。给定最近几轮对话历史和用户当前这一句话，"
    "判断当前这句话脱离历史后是否仍能独立理解、执行。\n\n"
    "把问题拆成三类槽位：\n"
    "- anchor：问的是哪个具体实体或实体类型\n"
    "- intent_type：问题的意图类型（计数/列举/查详情/比较）\n"
    "- constraint：过滤/限定条件（属于哪个公司、大于多少等）\n\n"
    "默认不改写：只有当前问题里某个槽位明显缺失、必须借助历史才能补全"
    "（比如用指代词'它/这个/上面提到的'代替了 anchor，或者只提到"
    "constraint 却没交代 intent_type），才判定为依赖历史。当前问题里已经"
    "显式出现的槽位（尤其是「多少个/数量/一共/共有」这类 intent_type=计数"
    "的措辞）必须原样保留，禁止被历史覆盖或省略。\n\n"
    "只输出 JSON：{\"rl\": 1或3, \"resolved_question\": \"...\", "
    "\"inherited_slots\": [...], \"duplicate_of\": \"...\"}\n"
    "rl=3（默认）：不依赖历史，resolved_question 必须逐字等于用户当前问题，"
    "inherited_slots 为空数组。\n"
    "rl=1：依赖历史，resolved_question 只补全缺失槽位对应的内容，不改写"
    "当前问题里已经出现的其余内容；inherited_slots 精确列出这次实际从"
    "历史补全了哪些槽位（anchor/intent_type/constraint 的子集，只填真正"
    "补全的，当前问题里本来就有的槽位不算继承）。\n"
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
    """历史指代消解（槽位粒度），顺带检测当前问题是否在问一个最近已经问过
    并回答过的问题（供设计 D 使用，同一次调用产出，不新增 LLM 调用）。

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
        return ResolvedQuestion(resolved_question=question, inherited_slots=[], duplicate_of=None)
    except Exception:
        logger.warning("resolve_question 调用失败，回退原始问题、无重复标记", exc_info=True)
        return ResolvedQuestion(resolved_question=question, inherited_slots=[], duplicate_of=None)
    try:
        payload = json.loads(result.text)
        resolved = str(payload.get("resolved_question") or "").strip() or question
        inherited_slots = [
            s for s in payload.get("inherited_slots") or []
            if s in ("anchor", "intent_type", "constraint")
        ]  # 不信任模型可能填出的其他字符串，只接受这三个已定义的槽位名
        duplicate_of = str(payload.get("duplicate_of") or "").strip() or None
    except json.JSONDecodeError:
        logger.warning("resolve_question 返回内容不是合法 JSON，回退原始问题、无重复标记")
        return ResolvedQuestion(resolved_question=question, inherited_slots=[], duplicate_of=None)
    return ResolvedQuestion(resolved_question=resolved, inherited_slots=inherited_slots, duplicate_of=duplicate_of)
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
    logger.info(
        "resolve_question: resolved=%r inherited_slots=%r duplicate_of=%r",
        result.resolved_question, result.inherited_slots, result.duplicate_of,
    )  # inherited_slots 目前没有下游消费，只落日志——留作复测时排查"槽位填充
       # 到底有没有生效、生效在哪个槽位"的可观测性入口，不是无意义的冗余记录。
    return {
        "resolved_question": result.resolved_question,
        "duplicate_of": result.duplicate_of,
    }
```

`graph.add_edge("memory_recall", "merge_after_parallel")` 改成 `graph.add_edge("memory_recall", "resolve_question")` + `graph.add_edge("resolve_question", "merge_after_parallel")`。

### `resolved_question` 的接入点：替换原始问题成为下游的"当前问题"

- `planner_node` 初始化 `planner_messages` 时，`messages.append({"role": "user", "content": state["question"]})` 改成 `state.get("resolved_question", state["question"])`（兜底：Layer 1 未跑过/失败时退回原始问题）。
- 各处构造 `ToolContext(question=state["question"], ...)` 的地方，改成 `question=state.get("resolved_question", state["question"])`——`context.question` 从原始问题变成已消解指代的版本，`structured_filter_query_tool` 的 `query_intent`/`is_verbatim` 生成逻辑不需要跟着改代码，只是看到的"当前问题"覆盖面更完整。
- 确定性检索路径（非 Planner 模式）的 `_PROMPT_TEMPLATE.format(context=context, question=state["question"])` 同样改用 `resolved_question`——这条路径同样有 `memory_context_messages` 注入，指代消解对它同样有价值。

### Layer 2：轮内转述保真（`is_verbatim` 强制显式决策，完全依赖提示词）

`structured_filter_query_tool` 的 `query_intent`/`is_verbatim` schema 改动，见下方"Schema 改动"和"代码改动"——`context.question` 现在是 Layer 1 消解后的问题，`resolve_arguments()` 本身不做任何确定性校验（见"2026-08-28 决策变更"）。

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

### 代码改动：`resolve_arguments()` 完全信任 `query_intent`/`is_verbatim`

`app/agent/tools/structured_filter_query/tool.py`：

```python
class StructuredFilterQueryTool:
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        query_intent = str(raw_arguments.get("query_intent") or "").strip() or context.question
        # 不做任何确定性核对——完全依赖 is_verbatim 这个强制显式决策字段
        # 本身（见下方"2026-08-28 决策变更"）和 query_intent 的 schema
        # 描述质量。is_verbatim 的值目前不参与任何分支逻辑，纯粹起"强制
        # 模型对'有没有改写'做一次显式承诺"这个作用——这个字段本身不校验，
        # 只是通过要求模型显式输出它，提高模型在生成 query_intent 时对
        # "是否忠实"这件事的注意力（类比 swiftagent `rl` 字段的效果，
        # 但这里没有配套的事后校验）。
        ...
```

**为什么只靠提示词，还要保留 `is_verbatim` 这个字段**：`is_verbatim` 不是校验机制，是"强制显式决策"这个提示词技巧本身——要求模型在输出 `query_intent` 的同时，必须显式回答"我有没有改写"这个问题，比单纯在 `query_intent` 的描述里写"默认不改写"更容易让模型认真对待这个判断（swiftagent `get_rewriting_decision_prompt_v2` 的核心机制正是"强制输出决策字段"这个动作本身，不是措辞多华丽）。但这终究是提示词层面的手段，不提供任何非 LLM 的确定性保障。

**已知风险**：本次会话已经验证过两次纯提示词调整（改字段描述、改 `trigger_cue`）对这个模型不够稳定可靠。去掉确定性核对后，`query_intent` 忠实转述这件事完全依赖提示词质量+`is_verbatim` 决策字段的引导效果，没有任何兜底——一旦提示词在某些措辞下仍然失效，"计数意图丢失"这类问题可能复现，且系统不会自动发现/拦截。这是本文档"2026-08-28 决策变更"里已经记录并确认过的取舍，不是遗漏。

### `_USAGE_GUIDE` 联动调整

`is_verbatim` 字段本身在 `manifest.yaml` 里已经说明了语义，不需要在 `_USAGE_GUIDE`（深层参数生成看到的详细说明）里重复展开——`_USAGE_GUIDE` 面向的是"拿到 query_intent 之后该怎么选 anchor 模式"，跟"query_intent 是怎么产生的"是两个不同阶段的关注点，不应该混在一起。

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
  - `tests/qa/test_query_rewrite.py` 新增 `resolve_question()` 的用例：无历史时零成本跳过（不发起 LLM 调用，`inherited_slots=[]`）；`rl=3` 时 `resolved_question` 逐字等于原文、`inherited_slots` 为空；`rl=1` 时正确补全缺失槽位、`inherited_slots` 列出实际继承的槽位子集；LLM 返回的 `inherited_slots` 里混入未定义的槽位名（比如模型幻觉输出了 `"time"`）时被过滤掉，只保留 `anchor`/`intent_type`/`constraint` 三者；LLM 失败/超时/解析失败时回退原文、`inherited_slots=[]`。**不测试"改写内容语义上对不对"这类用例**——没有确定性核对逻辑，`resolved_question` 就是模型返回什么就是什么，测试只覆盖结构化解析/异常处理这些确定性行为。
  - `tests/agent/test_graph.py`（或等价文件）新增：`resolve_question_node` 正确写入 `state["resolved_question"]`；`planner_node`/`ToolContext` 构造正确使用 `resolved_question` 而非原始 `question`。
  - `tests/agent/tools/test_structured_filter_query.py` 现有的 `is_verbatim`/关键词核对用例需要删除或改写——不再有"关键词丢失时回退原文"这类可断言的确定性行为，只能测"`is_verbatim`/`query_intent` 字段被正确传递到 prompt 里"这类结构性行为。
- 设计 C：`tests/qa/test_query_rewrite.py` 新增/补全"原始问题已清晰，不改写"用例；确认 `conversation_context` 参数不再从 `vector_search_tool.execute()` 传入。
- 设计 D：见上方"设计 D"小节的测试。
- 全部改动后跑一次全量测试套件确认无回归。

## 范围外

- `resolve_term()`（`anchor.name` 模式下的精确匹配）不在本次设计范围内——它服务的目的（"commit 到唯一节点前的最后一道精确校验"）跟本文档讨论的问题都不同，本次会话已经确认它的策略（精确匹配、不模糊）是合理的，不需要跟着改。
- ETL 配置（`产品→公司` 关系建模方式，多对多语义）不在本次设计范围内——这是数据源/建模层面的独立问题，本次会话已经分析过，不属于查询匹配/改写逻辑的范畴。
- 设计 D 的"重复问题检测"只做软提示，不做自动答案复用/自动短路工具调用——如果后续观察到 Planner 对软提示的采纳率不理想（比如总是选择重新查询，提示形同虚设），是否要升级成更强的机制（比如更高置信度时的自动短路），留作后续单独评估，本次不做。
