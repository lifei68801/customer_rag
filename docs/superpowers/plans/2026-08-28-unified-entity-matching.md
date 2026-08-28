# 统一实体匹配算法 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `term_matcher` 和 `ontology_recall` 共享同一套底层字符匹配计算，消除"同一段文本两处给出矛盾结论"，并修复召回侧"短候选名虚高"导致召回错误实体的问题。

**Architecture:** 新增一个共享底层函数 `matched_length()`（带间隔约束的字符级最长公共子序列）放在 `app/graphrag/term_matcher.py`；`ontology_recall.py` 反向依赖它（`term_matcher.py` 不 import `ontology_recall.py`，避免循环依赖）。两侧各自做适合自己的归一化：`term_matcher` 用滑动窗口 + 简单比例（窗口长度恒等于候选名长度，P=R，无需 F-beta）；`ontology_recall` 用全 query + F-beta 双向评分（β=0.5，用 R 项惩罚短候选名虚高）。

**Tech Stack:** Python 3.12、pytest（`.venv/Scripts/python.exe`，Windows）

**Spec:** `docs/superpowers/specs/2026-08-27-query-matching-and-rewrite-redesign-design.md`（设计 A 一节）

## Global Constraints

- 运行 Python/pytest 一律用 `.venv/Scripts/python.exe`（本机是 Windows，没有 `.venv/bin/python`）。
- 运行任何输出中文的 Python 命令必须加 `PYTHONIOENCODING=utf-8` 前缀，否则 Windows 控制台 `cp1252` 编码会抛 `UnicodeEncodeError`。
- `matched_length()` 的默认间隔常量取 `_DEFAULT_INTERVAL = 2`（实测甜点值，3 太松、1 太紧）。
- `ontology_recall` 的 F-beta 取 `_FBETA = 0.5`（实测：β=0.2 时 Cola 仍高于 Coca-Cola）。
- `term_matcher.match_terms()` 的 `fuzzy_threshold` 默认值保持 `0.75` 不变。
- `match_terms()` 的精确匹配分支（`candidate.lower() in text_lower`）完全不改动。
- 提交信息用英文，结尾附带：
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC
  ```

---

### Task 1: 新增共享底层函数 `matched_length()`

**Files:**
- Modify: `app/graphrag/term_matcher.py`（当前 65 行，在 `_has_fuzzy_match` 之前插入新函数和常量）
- Test: `tests/graphrag/test_term_matcher.py`（当前 141 行，在文件末尾追加）

**Interfaces:**
- Consumes: 无（本任务是最底层）
- Produces: `app.graphrag.term_matcher.matched_length(a: str, b: str, *, interval: int = 2) -> int`
  和模块级常量 `_DEFAULT_INTERVAL = 2`。后续 Task 2 和 Task 3 都依赖这个函数。

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_term_matcher.py` 文件**末尾**追加（注意文件顶部已有 `from app.graphrag.term_matcher import match_terms`，需要改成同时导入 `matched_length`）：

先把第 2 行的 import 改成：
```python
from app.graphrag.term_matcher import matched_length, match_terms
```

然后在文件末尾追加：
```python
def test_matched_length_counts_in_order_characters_skipping_one_typo():
    # "服务器链接超时" 相对 "服务器连接超时" 只错了「连→链」一个字，
    # 按顺序能匹配上其余 6 个字（服务器+接超时）。最长公共【子串】只能
    # 数出「服务器」=3（后面的「接超时」被错字隔断成另一段而丢弃），
    # 这正是这个函数要解决的问题。
    assert matched_length("服务器链接超时", "服务器连接超时") == 6


def test_matched_length_distinguishes_one_typo_from_two():
    # 错1字能匹配6个，错2字只能匹配5个——这个差异是 term_guard 判定
    # "该不该命中" 的全部依据，必须被保留下来。
    one_typo = matched_length("服务器链接超时", "服务器连接超时")
    two_typos = matched_length("服务器网络超时", "服务器连接超时")
    assert one_typo == 6
    assert two_typos == 5
    assert one_typo > two_typos


def test_matched_length_interval_blocks_characters_scattered_across_text():
    # 「服务器连接超时」这 7 个字确实全部按顺序出现在这句无关的话里
    # （服-务-...-器-...-连-...-接-...-超-...-时），不加间隔约束的话
    # 会被判成完全匹配（7），这是纯最长公共子序列的致命误匹配。
    # interval=2 要求相邻两个匹配字符之间最多只能跨 2 个字符，把这种
    # 散落匹配切断。
    decoy = "服务里有个器件，连着接口，超过时限了"
    assert matched_length(decoy, "服务器连接超时", interval=99) == 7
    assert matched_length(decoy, "服务器连接超时", interval=2) < 7


def test_matched_length_is_case_insensitive():
    assert matched_length("COKE-COLA", "coke-cola") == 9


def test_matched_length_returns_zero_for_empty_input():
    assert matched_length("", "abc") == 0
    assert matched_length("abc", "") == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_term_matcher.py -q`
Expected: FAIL，报 `ImportError: cannot import name 'matched_length' from 'app.graphrag.term_matcher'`

- [ ] **Step 3: 实现 `matched_length()`**

在 `app/graphrag/term_matcher.py` 里，把第 1-6 行（`from __future__` 到 `from app.graphrag.ontology import Term` 那段 import）之后、`def _has_fuzzy_match` 之前，插入常量和新函数：

```python
_DEFAULT_INTERVAL = 2


def matched_length(a: str, b: str, *, interval: int = _DEFAULT_INTERVAL) -> int:
    """b 的字符按顺序在 a 里最多能匹配上多少个，且相邻两个匹配字符在 a 中
    跨越的字符数不超过 interval。大小写不敏感。

    这是"最长公共子序列"加了间隔约束的版本。不加约束时，长文本里散落各处
    的字符也能被全部匹配上——实测"服务里有个器件，连着接口，超过时限了"
    对"服务器连接超时"能匹配满 7 个字，是纯子序列算法的致命误匹配来源；
    interval 把匹配限制在局部连贯的区域内。

    间隔取 2 是实测甜点：取 3 太松，压不掉上面那个散落误匹配；取 1 太紧，
    会误伤 "coke-cola"→"Coca-Cola" 这类正常拼写变体。机制借鉴 swiftagent
    dev/2.7.5 的 get_common_str.py::_get_max_sequence（那里是 token 级
    interval=3，这里按字符级、取 2）。

    为什么不用 difflib.SequenceMatcher 或最长公共【子串】：
    - 最长公共子串只保留单个最长连续片段，"服务器链接超时"和"服务器网络
      超时"对"服务器连接超时"都只能数出「服务器」=3，无法区分错1字和错2字。
    - SequenceMatcher 能区分，但它是 term_guard 侧的老实现，没有间隔约束
      这个旋钮，也无法被召回侧复用（召回侧要的是"匹配了多少个字符"这个
      原始量，好在上层套 F-beta，而不是一个已经归一化死的比值）。

    dp[i][j] = 「x[i-1] 与 y[j-1] 配对、且这是最后一个匹配」时能达到的最大匹配数。
    必须把"最后一个匹配落在哪"编进状态本身，而不是像 dp[i][j]=前i前j的最大
    匹配数、再另外用一张 last[i][j] 记录位置——后者不是合法的 DP：在某个
    (i,j) 上贪心取最大长度，可能留下一个对后续间隔检查更不利的 last，导致
    整体反而更差。实测反例 matched_length("bbbbbbba", "baba", interval=2)
    在那种写法下返回 2，真实最优是 3。

    prefix_best[i][j] = max(dp[i][1..j])，让"上一个匹配落在 x 的第 i 个字符、
    且只用掉 y 的前 j 个字符"这一族状态能 O(1) 查到最好成绩。间隔约束
    (i-1) - (i2-1) - 1 <= interval 化简成 i2 >= i - interval - 1，所以往回
    只需要看 interval+1 个位置。
    """
    x, y = a.lower(), b.lower()
    if not x or not y:
        return 0
    n, m = len(x), len(y)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    prefix_best = [[0] * (m + 1) for _ in range(n + 1)]
    answer = 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if x[i - 1] == y[j - 1]:
                # 上一个匹配要么不存在（这是起点，prev=0），要么落在
                # x 的 [i-interval-1, i-1] 号位置上。
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

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_term_matcher.py -q`
Expected: PASS，17 passed（原有 12 条 + 新增 5 条）

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/term_matcher.py tests/graphrag/test_term_matcher.py
git commit -m "feat(graphrag): add interval-constrained subsequence matcher as shared primitive

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```

---

### Task 2: `_has_fuzzy_match()` 改用 `matched_length()`

**Files:**
- Modify: `app/graphrag/term_matcher.py:8-23`（Task 1 插入新函数后，`_has_fuzzy_match` 的行号会后移，按函数名定位）
- Test: `tests/graphrag/test_term_matcher.py`（现有 12 条测试作为回归网，不新增测试）

**Interfaces:**
- Consumes: `matched_length(a, b, *, interval=2) -> int`（Task 1 产出）
- Produces: 无新接口，`_has_fuzzy_match(text, candidate, *, threshold) -> bool` 签名不变

- [ ] **Step 1: 先跑一遍现有测试，确认改动前是绿的**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_term_matcher.py -q`
Expected: PASS，17 passed（这一步是拿到改动前的基线，不是 TDD 的红灯——本任务是等价替换，现有 12 条测试就是它的验收标准）

- [ ] **Step 2: 替换 `_has_fuzzy_match()` 的实现**

把 `app/graphrag/term_matcher.py` 里整个 `_has_fuzzy_match` 函数（从 `def _has_fuzzy_match` 到 `return False`）替换成：

```python
def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
    """滑动窗口 + 带间隔约束的子序列匹配，判断 candidate 是否在 text 里有
    足够相似的片段。

    窗口长度等于候选词长度，逐位置滑动。因为窗口长度恒等于 len(candidate)，
    这里的"匹配长度/窗口长度"和"匹配长度/候选名长度"是同一个值——也就是
    precision 恒等于 recall，所以不需要像召回侧（ontology_recall.
    fbeta_match_score）那样引入 F-beta 双向评分，直接用比例即可。
    """
    window = len(candidate)
    if window == 0 or len(text) < window:
        return False
    for i in range(len(text) - window + 1):
        if matched_length(text[i : i + window], candidate) / window >= threshold:
            return True
    return False
```

同时删除文件顶部已经不再使用的 `import difflib`（第 3 行）。

- [ ] **Step 3: 更新 `match_terms()` docstring 里对旧算法的描述**

`match_terms()` 的 docstring 里有两处提到 difflib，需要同步更新，否则文档与实现不符。把这两段：

```python
    这是 TermGuard 强制安全网的第一层判断。精确匹配（字面子串出现）
    优先；某个术语的所有候选名都没有精确命中时，再用滑动窗口 +
    difflib.SequenceMatcher 相似度兜底一次。模糊命中不经过 LLM 二次
```

改成：

```python
    这是 TermGuard 强制安全网的第一层判断。精确匹配（字面子串出现）
    优先；某个术语的所有候选名都没有精确命中时，再用滑动窗口 +
    matched_length（带间隔约束的子序列匹配）兜底一次。模糊命中不经过 LLM 二次
```

以及把：

```python
    已知代价：短码型候选（如错误码 "E502"，4 个字符）在 0.75 阈值下
    分辨率很低——只差 1 位的同族码（如 "E503"）相似度恰好等于 0.75，
```

改成：

```python
    已知代价：短码型候选（如错误码 "E502"，4 个字符）在 0.75 阈值下
    分辨率很低——只差 1 位的同族码（如 "E503"）匹配比例恰好等于 0.75
    （4 个字符里匹配上 3 个），
```

- [ ] **Step 4: 运行测试确认全部通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_term_matcher.py -q`
Expected: PASS，17 passed。

**如果有任何一条失败就停下来报告，不要调整阈值让它变绿**——设计阶段已实测确认这 12 条现有用例在新算法 + 阈值 0.75 下全部通过，出现失败说明实现与设计有偏差。

- [ ] **Step 5: 确认 difflib 已经不再被引用**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "import ast,sys; src=open('app/graphrag/term_matcher.py',encoding='utf-8').read(); print('difflib' in src)"`
Expected: 输出 `False`

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/term_matcher.py
git commit -m "refactor(graphrag): switch term_matcher fuzzy layer to the shared matcher

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```

---

### Task 3: `ontology_recall` 改用 F-beta 双向评分

**Files:**
- Modify: `app/graphrag/ontology_recall.py`（新增 `fbeta_match_score()`；改 `_best_score()` 内的打分调用；`longest_common_substring_score`/`_ngram_score_prelowered` 保留但不再被 `_best_score` 使用）
- Test: `tests/graphrag/test_ontology_recall.py`（当前 267 行，末尾追加）

**Interfaces:**
- Consumes: `app.graphrag.term_matcher.matched_length(a, b, *, interval=2) -> int`（Task 1 产出）
- Produces: `app.graphrag.ontology_recall.fbeta_match_score(query: str, name: str, *, beta: float = 0.5) -> float`
  和模块级常量 `_FBETA = 0.5`

- [ ] **Step 1: 写失败的测试**

在 `tests/graphrag/test_ontology_recall.py` **文件末尾**追加：

```python
def test_fbeta_match_score_ranks_full_company_name_above_short_product_name():
    # 2026-08-27 真实 bug 的核心成因：`Cola`(4字) 是 `Coca-Cola`(9字) 的子串，
    # 任何单向的"匹配长度/len(name)"公式，Cola 都拿满分 1.0 碾压 Coca-Cola，
    # 导致召回把错误的产品实体排在正确的公司实体前面。F-beta 的 R 项
    # （匹配长度/len(query)）惩罚"query 很长却只匹配上一小段"的短候选名，
    # 把这个排序扭转过来。
    from app.graphrag.ontology_recall import fbeta_match_score

    query = "coke-cola公司有多少个订单"
    assert fbeta_match_score(query, "Coca-Cola") > fbeta_match_score(query, "Cola")


def test_fbeta_match_score_gives_zero_for_unrelated_name():
    from app.graphrag.ontology_recall import fbeta_match_score

    assert fbeta_match_score("coke-cola公司有多少个订单", "服务器连接超时") == 0.0


def test_fbeta_match_score_treats_whole_name_containment_as_full_match():
    # 候选名整段出现在 query 里时按满额匹配算（保留现有 _best_score 的
    # containment 快速路径语义），此时 precision 应该是 1.0，最终分只受
    # recall 项影响、不应该被间隔约束打折。
    from app.graphrag.ontology_recall import fbeta_match_score

    assert fbeta_match_score("我想查 Coca-Cola 的订单", "Coca-Cola") > 0.5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_recall.py -q`
Expected: FAIL，报 `ImportError: cannot import name 'fbeta_match_score' from 'app.graphrag.ontology_recall'`

- [ ] **Step 3: 实现 `fbeta_match_score()`**

在 `app/graphrag/ontology_recall.py` 里，第 9 行 `from app.graphrag.ontology_constraints import AllowedCombination` 之后追加 import：

```python
from app.graphrag.term_matcher import matched_length
```

在 `_PATH_TOP_K = 10` 那组常量之后追加：

```python
_FBETA = 0.5
```

在 `longest_common_substring_score()` 函数之后追加新函数：

```python
def fbeta_match_score(query: str, name: str, *, beta: float = _FBETA) -> float:
    """query 与候选名 name 的匹配分，用 F-beta 融合两个方向的比例：
    P（精确率）= 匹配长度/len(name)，衡量"匹配覆盖了候选名多少内容"；
    R（召回率）= 匹配长度/len(query)，衡量"匹配占 query 多大比例"。

    必须用双向 F-beta 而不是单向的 P：`Cola`(4字) 是 `Coca-Cola`(9字) 的
    子串，只看 P 的话 Cola 永远拿满分 1.0 碾压 Coca-Cola——这正是
    2026-08-27 "coke-cola公司有多少个订单" 召回到错误实体（产品 Cola 而
    不是公司 Coca-Cola）的成因之一。R 项让"query 很长却只匹配上一小段"的
    短候选名掉分。

    beta=0.5 是实测取值：beta<1 仍然偏向精确率（这是召回场景该有的倾向），
    但要大到足以让 R 惩罚生效——实测 beta=0.2（swiftagent dev/2.7.5 的
    默认值，见 get_common_str.py::_get_match_score）时 Cola 0.8889 仍然
    高于 Coca-Cola 0.7521；beta=0.5 时 Coca-Cola 0.6604 反超 Cola 0.6061。

    候选名整段出现在 query 里时直接按满额匹配计算，跳过间隔约束——这段
    保留 _best_score 原有的 containment 快速路径语义：整段命中是最强的
    信号，不该因为候选名内部字符间隔而被打折。
    """
    if not name or not query:
        return 0.0
    if name.lower() in query.lower():
        matched = len(name)
    else:
        matched = matched_length(query, name)
    if matched == 0:
        return 0.0
    precision = matched / len(name)
    recall = matched / len(query)
    b2 = beta * beta
    return (1 + b2) * precision * recall / (b2 * precision + recall)
```

- [ ] **Step 4: 运行新测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_recall.py -q -k fbeta`
Expected: PASS，3 passed

- [ ] **Step 5: 把 `_best_score()` 切换到新打分函数**

`_best_score()` 现在有两条打分路径（containment 快速路径 + n-gram 循环），新的 `fbeta_match_score()`
内部已经包含 containment 判断，所以整个函数简化成：对每个候选名直接算一次 `fbeta_match_score`，
取最大值。bigram 预过滤保留（性能优化）。

把整个 `_best_score` 函数替换成：

```python
def _best_score(query_text: str, ngrams: list[str], query_bigrams: set[str], *names: str) -> float:
    """对多个候选名字（比如一个关系三元组的三个组成部分）分别打分，取最高
    的一个——命中任意一个组成部分就算这个候选跟 query 相关，不要求全部命中。

    打分委托给 fbeta_match_score（见那里的说明：为什么必须是双向 F-beta 而
    不是单向比例）。它内部已经包含"候选名整段出现在 query 里"的快速路径，
    所以这里不再单独做 containment 检查。

    bigram 预过滤保留：两个字符串如果没有任何公共的 2 字符子串，匹配长度
    必然很小、不可能打出及格分，可以直接跳过后面 O(|query|·|name|) 的 DP。
    这个预过滤只是性能优化，不改变任何能及格的候选的得分。ngrams 参数不再
    参与打分（fbeta_match_score 直接吃完整 query，间隔约束替代了 n-gram
    切分原本承担的"限制匹配局部性"作用），保留在签名里是为了不改动全部
    调用方；query_bigrams 仍然用于预过滤。
    """
    if not names:
        return 0.0
    best = 0.0
    for name in names:
        lowered_name = name.lower()
        name_bigrams = {lowered_name[i : i + 2] for i in range(len(lowered_name) - 1)}
        if name_bigrams and name_bigrams.isdisjoint(query_bigrams):
            continue
        score = fbeta_match_score(query_text, name)
        if score > best:
            best = score
    return best
```

- [ ] **Step 6: 运行 ontology_recall 全部测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_recall.py -q`
Expected: PASS。

**如果有失败，先看失败的是哪一条**：`test_longest_common_substring_score_*` 这几条测的是
`longest_common_substring_score()` 本身（函数保留未删，应该照常通过）；如果失败的是
`test_recall_ontology_candidates_*` 系列，说明换算法后某个候选的得分掉到 `_MIN_SCORE`(0.3)
以下或排序变了，把实际得分打出来贴到报告里，不要直接改 `_MIN_SCORE` 让它变绿。

- [ ] **Step 7: 提交**

```bash
git add app/graphrag/ontology_recall.py tests/graphrag/test_ontology_recall.py
git commit -m "fix(graphrag): score recall candidates with two-sided F-beta

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```

---

### Task 4: 跨模块一致性回归测试

**Files:**
- Test: `tests/graphrag/test_term_matcher.py`（末尾追加）

**Interfaces:**
- Consumes: `match_terms()`（Task 2 改造后）、`recall_ontology_candidates()`（Task 3 改造后）
- Produces: 无

- [ ] **Step 1: 写测试**

在 `tests/graphrag/test_term_matcher.py` 末尾追加：

```python
def test_match_terms_and_recall_agree_on_the_coke_cola_case():
    """两处匹配逻辑对同一段文本必须给出一致结论——这是"统一实体匹配"这个
    设计目标的直接回归锚点。

    2026-08-27 的真实 bug：同一句 "coke-cola公司有多少个订单"，term_guard
    只匹配到产品 Cola（漏掉公司 Coca-Cola）、把错误实体的图谱上下文强制
    注入，而召回侧其实两个都能召回——两处结论矛盾，且系统里没有任何一处
    会发现这种矛盾。
    """
    from app.graphrag.ontology_categories import TermTypeCategory
    from app.graphrag.ontology_recall import recall_ontology_candidates

    terms = [
        Term(tenant_id="demo", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
             aliases=[], term_type="公司"),
        Term(tenant_id="demo", node_key="产品:Cola", standard_name="Cola",
             aliases=[], term_type="产品"),
    ]
    question = "coke-cola公司有多少个订单"

    guard_hits = {t.standard_name for t in match_terms(question, terms)}
    recalled = {t.standard_name for t in recall_ontology_candidates(
        question, terms=terms,
        term_type_schema={
            "公司": TermTypeCategory(value="公司", extra_fields=[]),
            "产品": TermTypeCategory(value="产品", extra_fields=[]),
        },
        allowed_combinations=[],
    ).entities}

    assert "Coca-Cola" in guard_hits
    assert "Coca-Cola" in recalled
```

文件顶部需要有 `from app.graphrag.ontology import Term`——检查一下，如果没有就加上。

- [ ] **Step 2: 运行测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/graphrag/test_term_matcher.py::test_match_terms_and_recall_agree_on_the_coke_cola_case -q`
Expected: PASS

- [ ] **Step 3: 跑全量测试套件**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`
Expected: PASS，全部通过（基线是 1327 passed，本计划新增 9 条测试，预期 1336 passed）

如果失败，把失败的测试名和输出完整贴进报告，不要自行放宽断言。

- [ ] **Step 4: 提交**

```bash
git add tests/graphrag/test_term_matcher.py
git commit -m "test(graphrag): pin cross-module agreement on the coke-cola case

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```
