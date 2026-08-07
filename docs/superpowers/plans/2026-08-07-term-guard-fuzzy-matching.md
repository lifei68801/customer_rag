# TermGuard 模糊匹配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `app/graphrag/term_matcher.py::match_terms()` 加一层模糊匹配兜底，让 TermGuard 除了精确子串命中之外，也能命中术语标准名/别名的近似变体（如打错一两个字的场景）。

**Architecture:** `match_terms()` 内部拆成"精确匹配（不变）+ 模糊匹配（仅对精确未命中的术语跑，滑动窗口+`difflib.SequenceMatcher`，阈值 0.75）"两层，对外的函数签名/返回契约（`text, terms -> list[Term]`，新增一个带默认值的可选关键字参数）保持兼容，调用方 `term_guard.py` 不需要改动。

**Tech Stack:** Python 3.12、stdlib `difflib`（不引入新依赖）、pytest（`asyncio_mode = "auto"`，本任务的测试都是同步函数，不需要 `async def`）。

## Global Constraints

- 严格 TDD：RED（写失败测试，确认失败原因正确）→ GREEN（最小实现）→ 跑全量测试 → git commit。
- 这是拆分出的 4 个独立子项目里的第 2 个（第 1 个"检索层修正"已完成并推送；后续还有输入/输出安全增强、GraphRAG 实体链接模糊匹配 2 个独立子项目，各自单独走完整流程，不在本计划范围内）。
- 模糊命中**不**引入 LLM 二次确认，直接和精确命中一样触发强制注入（已确认的设计决策）——不要在实现里加任何 LLM 调用。
- **不新增 Settings 配置项**，`fuzzy_threshold` 是函数关键字参数的硬编码默认值 `0.75`（沿用 `app/voice/asr_term_correction.py::correct_asr_terms` 的 `fuzzy_threshold` 参数从未被调用方覆盖、只作为函数默认值存在的既定约定）。
- `match_terms()` 的调用方 `app/graphrag/term_guard.py` **不需要改动**——它现在调用 `match_terms(text, terms)`（不传 `fuzzy_threshold`），改动后依然可以这样调用，函数内部行为变化对它透明。
- 不改动 GraphRAG 实体链接（`app/graphrag/normalization.py::resolve_to_standard_name`）——那是拆分出的第 4 个独立子项目，不在本计划范围。
- Commit message 格式：一行摘要（`feat:`/`fix:` 前缀）+ 空行 + 中文详细说明（为什么这么做/复用了什么/刻意不做什么）+ 以 `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` 结尾。
- 本仓库当前在 `dev/0.1` 分支直接工作，不建 worktree。
- 测试命令统一用 `.venv/Scripts/python.exe -m pytest <path> -v`（Windows 环境，本仓库自带 `.venv`）。
- 设计依据：`docs/superpowers/specs/2026-08-07-term-guard-fuzzy-matching-design.md`（已经用户批准，不要偏离其中的机制决策）。

---

### Task 1: `match_terms()` 加模糊匹配层

**Files:**
- Modify: `app/graphrag/term_matcher.py`（整个文件，当前只有 19 行）
- Test: `tests/graphrag/test_term_matcher.py`（当前有 3 个精确匹配测试，全部保持不变、必须继续通过）

**Interfaces:**
- Consumes：无新依赖（`app.graphrag.ontology.Term` 已有，stdlib `difflib`）。
- Produces：`match_terms(text: str, terms: list[Term], *, fuzzy_threshold: float = 0.75) -> list[Term]`——外部契约不变（`term_guard.py` 现有的 `match_terms(text, terms)` 调用继续有效），仅新增一个带默认值的可选关键字参数。这是本次唯一的公开接口改动，后续任何任务都不依赖这个函数的内部实现细节（只有这一个任务）。

- [ ] **Step 1: 写失败测试**

把 `tests/graphrag/test_term_matcher.py` 整个文件替换为（在原有 3 个测试后追加 3 个新测试，原有测试内容一字不改）：

```python
from app.graphrag.ontology import Term
from app.graphrag.term_matcher import match_terms

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时", "E502"],
        term_type="error_code",
        product_line="核心平台",
    ),
    Term(
        standard_name="登录模块",
        aliases=["认证模块", "登录"],
        term_type="module",
        product_line="核心平台",
    ),
]


def test_match_terms_finds_standard_name_via_alias():
    matches = match_terms("我这边报了网关超时，应该怎么办", _TERMS)

    assert [m.standard_name for m in matches] == ["错误码E502"]


def test_match_terms_returns_empty_when_no_alias_or_name_present():
    matches = match_terms("今天天气怎么样", _TERMS)

    assert matches == []


def test_match_terms_dedupes_when_multiple_aliases_of_same_term_present():
    matches = match_terms("E502 也就是网关超时的意思吧", _TERMS)

    assert [m.standard_name for m in matches] == ["错误码E502"]


_FUZZY_TERMS = [
    Term(
        standard_name="服务器连接超时",
        aliases=[],
        term_type="error_code",
        product_line="核心平台",
    ),
]


def test_match_terms_finds_fuzzy_variant_within_threshold():
    # "连接" 打成了同音的"链接"，7 个字里错 1 个字，difflib.SequenceMatcher
    # 相似度约 0.857（(7-1)/7），高于默认阈值 0.75，应该被模糊层命中。
    matches = match_terms("最近老是提示服务器链接超时，是不是坏了", _FUZZY_TERMS)

    assert [m.standard_name for m in matches] == ["服务器连接超时"]


def test_match_terms_does_not_fuzzy_match_below_threshold():
    # 完全不相关的文本，相似度接近 0，远低于阈值，不应该被误命中。
    matches = match_terms("今天天气怎么样", _FUZZY_TERMS)

    assert matches == []


def test_match_terms_does_not_duplicate_exact_match_via_fuzzy_layer():
    # 文本里已经精确包含标准名，结果里这个术语只应该出现一次
    # （不会因为同时被模糊层重复命中而出现两次）。
    matches = match_terms("服务器连接超时了，麻烦看一下", _FUZZY_TERMS)

    assert [m.standard_name for m in matches] == ["服务器连接超时"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_term_matcher.py -v`
Expected: 原有 3 个测试仍然通过（还没改实现，精确匹配行为不变）；新增的 `test_match_terms_finds_fuzzy_variant_within_threshold` 失败（`assert [] == ['服务器连接超时']`，因为当前实现没有模糊匹配层，"服务器链接超时"不是"服务器连接超时"的精确子串，找不到任何命中）。`test_match_terms_does_not_fuzzy_match_below_threshold` 和 `test_match_terms_does_not_duplicate_exact_match_via_fuzzy_layer` 这两个此时应该已经通过（当前实现本来就不会误命中，也不会重复），这是正常的——它们是为了在 Step 4 之后继续保护这两条行为不被破坏，不是这一步用来证明"功能缺失"的失败用例。

- [ ] **Step 3: 写最小实现**

把 `app/graphrag/term_matcher.py` 整个文件替换为：

```python
from __future__ import annotations

import difflib

from app.graphrag.ontology import Term


def _has_fuzzy_match(text: str, candidate: str, *, threshold: float) -> bool:
    """滑动窗口 + difflib 相似度，判断 candidate 是否在 text 里有足够相似的片段。

    窗口长度等于候选词长度，逐位置滑动计算相似度比值——算法复用
    app/voice/asr_term_correction.py::_find_fuzzy_candidates 的核心逻辑。
    """
    window = len(candidate)
    if window == 0 or len(text) < window:
        return False
    for i in range(len(text) - window + 1):
        span = text[i : i + window]
        ratio = difflib.SequenceMatcher(None, span, candidate).ratio()
        if ratio >= threshold:
            return True
    return False


def match_terms(
    text: str, terms: list[Term], *, fuzzy_threshold: float = 0.75
) -> list[Term]:
    """精确匹配 + 模糊匹配兜底：文本中出现术语的标准名称或任一别名
    （原样出现，或足够相似）即命中该术语。

    这是 TermGuard 强制安全网的第一层判断。精确匹配（字面子串出现）
    优先；某个术语的所有候选名都没有精确命中时，再用滑动窗口 +
    difflib.SequenceMatcher 相似度兜底一次。模糊命中不经过 LLM 二次
    确认，直接和精确命中一样触发强制注入——TermGuard 误命中的代价
    很轻（多塞一段可能不相关的图谱上下文，不像 ASR 校正那样直接
    改写用户输入文本），保持"TermGuard 不依赖 LLM 自主判断"这条
    架构设计的核心原则。

    fuzzy_threshold 默认 0.75（比 ASR 校正的 0.6 更保守，因为这里没有
    LLM 兜底误召回）——这是参考起点，需要结合真实数据调整，不是权威值。
    """
    matched: list[Term] = []
    for term in terms:
        candidates = [term.standard_name, *term.aliases]
        if any(candidate and candidate in text for candidate in candidates):
            matched.append(term)
            continue
        if any(
            candidate
            and _has_fuzzy_match(text, candidate, threshold=fuzzy_threshold)
            for candidate in candidates
        ):
            matched.append(term)
    return matched
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_term_matcher.py -v`
Expected: 6 passed（原有 3 个 + 新增 3 个）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 除了 1 个已知的、与本次改动完全无关的预先存在失败（`tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`，本地 `.env` 环境里真实 TTS 凭证泄漏导致的环境问题，在这次会话之前的多轮计划里已反复确认和本次代码改动无关）之外，全部通过。因为 `match_terms()` 的外部契约没变（新参数带默认值，`term_guard.py` 不需要跟着改），不应该有任何其他测试因为这个改动失败——如果发现有，说明存在本计划没预见到的依赖，需要先排查再继续，不要跳过。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/term_matcher.py tests/graphrag/test_term_matcher.py
git commit -m "feat: add fuzzy matching fallback to TermGuard's match_terms"
```

---

## 完成后

任务提交后，TermGuard 除了精确命中术语标准名/别名之外，也能兜底命中近似变体（如打错一两个字的场景），不引入额外的 LLM 调用或延迟，`term_guard.py` 调用方无感知。架构覆盖度审计标记的这一项行为偏差解决。后续 2 个子项目（输入/输出安全增强、GraphRAG 实体链接模糊匹配）按此前确认的顺序各自独立走完整流程，不在本计划范围内。
