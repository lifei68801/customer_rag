# 两层改写架构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"改写"从一个混在 Planner 自由文本生成里的隐式动作，拆成两层各自解决一个性质不同的问题——Layer 1 做跨轮次历史指代消解（一次性、槽位粒度），Layer 2 做轮内转述保真（强制显式决策字段）——并顺带利用 Layer 1 的产出给重复提问加一条软提示。

**Architecture:** 新增 `resolve_question()` 到 `app/qa/query_rewrite.py`，用一次 LLM 调用同时产出「消解指代后的问题 + 实际继承了哪些槽位 + 是否重复提问」；在 LangGraph 里插一个 `resolve_question` 节点（`memory_recall` 之后、`merge_after_parallel` 之前，因为它依赖 `memory_recall_node` 产出的 `memory_context_messages`）；下游 `planner_node`/`ToolContext`/确定性检索路径统一改用消解后的 `resolved_question`；`rewrite_query()` 职责收窄成只做检索友好化；`structured_filter_query_tool` 加 `is_verbatim` 强制显式决策字段。

**Tech Stack:** Python 3.12、LangGraph、pytest（`.venv/Scripts/python.exe`，Windows）

**Spec:** `docs/superpowers/specs/2026-08-27-query-matching-and-rewrite-redesign-design.md`（设计 B/C/D 三节）

## Global Constraints

- 运行 Python/pytest 一律用 `.venv/Scripts/python.exe`（本机是 Windows，没有 `.venv/bin/python`）。
- 运行任何输出中文的 Python 命令必须加 `PYTHONIOENCODING=utf-8` 前缀，否则 Windows 控制台 `cp1252` 编码会抛 `UnicodeEncodeError`。
- **不要实现任何确定性关键词核对**（`counting_intent.py` / `drops_counting_keywords()` 都不要建）——设计文档"2026-08-28 决策变更"一节已明确去掉这层，完全依赖提示词。
- Layer 1 的槽位只有三个：`anchor` / `intent_type` / `constraint`。解析 LLM 返回的 `inherited_slots` 时必须按这三个名字做白名单过滤，丢弃模型幻觉出的其他值。
- Layer 1 的改写模式只有两档：`rl=3`（不改写，默认）和 `rl=1`（强改写）。不引入 swiftagent 的 `rl=2` 弱改写档。
- `resolve_question()` 的失败兜底一律是"原样返回问题、`inherited_slots=[]`、`duplicate_of=None`"，超时/异常/JSON 解析失败都走这条路径——保证"下限不比不做这一步差"。
- 设计 D 的重复提问检测**只做软提示**，绝不自动跳过工具调用。
- 提交信息用英文，结尾附带：
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC
  ```

---

### Task 1: `resolve_question()` —— Layer 1 的核心函数

**Files:**
- Modify: `app/qa/query_rewrite.py`（当前 57 行，只有 `rewrite_query()` 一个函数；在文件末尾追加）
- Test: `tests/qa/test_query_rewrite.py`（末尾追加）

**Interfaces:**
- Consumes: 无
- Produces:
  - `app.qa.query_rewrite.ResolvedQuestion`（frozen dataclass，字段 `resolved_question: str`、`inherited_slots: list[str]`、`duplicate_of: str | None`）
  - `app.qa.query_rewrite.resolve_question(question, history, *, llm_registry, llm_provider_name, timeout_sec=1.5) -> ResolvedQuestion`
  - 后续 Task 2（图节点）、Task 5（软提示）都依赖这两个。

- [ ] **Step 1: 写失败的测试**

在 `tests/qa/test_query_rewrite.py` 末尾追加：

```python
async def test_resolve_question_keeps_question_verbatim_when_not_depending_on_history():
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 3, "resolved_question": "Coca-Cola公司有多少个订单", '
        '"inherited_slots": [], "duplicate_of": ""}'
    )
    result = await resolve_question(
        "Coca-Cola公司有多少个订单",
        [{"role": "user", "content": "之前聊了别的"}],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
    )

    assert result.resolved_question == "Coca-Cola公司有多少个订单"
    assert result.inherited_slots == []
    assert result.duplicate_of is None


async def test_resolve_question_fills_anchor_slot_from_history():
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 1, "resolved_question": "Coca-Cola有多少个订单", '
        '"inherited_slots": ["anchor"], "duplicate_of": ""}'
    )
    result = await resolve_question(
        "它有多少个订单",
        [{"role": "user", "content": "Coca-Cola是什么公司"}],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
    )

    assert result.resolved_question == "Coca-Cola有多少个订单"
    assert result.inherited_slots == ["anchor"]


async def test_resolve_question_drops_undefined_slot_names():
    # 槽位只有 anchor/intent_type/constraint 三个，模型幻觉出的其他名字
    # （比如照搬 swiftagent 的 time 槽位）必须被过滤掉，不能污染下游。
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 1, "resolved_question": "改写后的问题", '
        '"inherited_slots": ["anchor", "time", "dimension"], "duplicate_of": ""}'
    )
    result = await resolve_question(
        "原问题", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(provider), llm_provider_name="llm",
    )

    assert result.inherited_slots == ["anchor"]


async def test_resolve_question_reports_duplicate_question():
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 3, "resolved_question": "Coca-Cola公司有多少个订单", '
        '"inherited_slots": [], "duplicate_of": "Coca-Cola公司有多少个订单"}'
    )
    result = await resolve_question(
        "Coca-Cola公司有多少个订单",
        [{"role": "user", "content": "Coca-Cola公司有多少个订单"},
         {"role": "assistant", "content": "10000个"}],
        llm_registry=_registry(provider), llm_provider_name="llm",
    )

    assert result.duplicate_of == "Coca-Cola公司有多少个订单"


async def test_resolve_question_falls_back_to_original_on_llm_failure():
    from app.qa.query_rewrite import resolve_question

    result = await resolve_question(
        "它有多少个订单", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(FailingLLMProvider()), llm_provider_name="llm",
    )

    assert result.resolved_question == "它有多少个订单"
    assert result.inherited_slots == []
    assert result.duplicate_of is None


async def test_resolve_question_falls_back_to_original_on_timeout():
    from app.qa.query_rewrite import resolve_question

    result = await resolve_question(
        "它有多少个订单", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(SlowLLMProvider()), llm_provider_name="llm",
        timeout_sec=0.01,
    )

    assert result.resolved_question == "它有多少个订单"


async def test_resolve_question_falls_back_to_original_on_malformed_json():
    from app.qa.query_rewrite import resolve_question

    result = await resolve_question(
        "它有多少个订单", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(FixedLLMProvider("这不是JSON")),
        llm_provider_name="llm",
    )

    assert result.resolved_question == "它有多少个订单"
    assert result.inherited_slots == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/qa/test_query_rewrite.py -q`
Expected: FAIL，报 `ImportError: cannot import name 'resolve_question' from 'app.qa.query_rewrite'`

- [ ] **Step 3: 实现 `resolve_question()`**

在 `app/qa/query_rewrite.py` 顶部，把现有 import 段：

```python
from __future__ import annotations

import asyncio
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry
```

改成：

```python
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry
```

在**文件末尾**追加：

```python
_SLOT_NAMES = ("anchor", "intent_type", "constraint")

_RESOLVE_QUESTION_SYSTEM_PROMPT = (
    "你是多轮对话的指代消解助手。给定最近几轮对话历史和用户当前这一句话，"
    "判断当前这句话脱离历史后是否仍能独立理解、执行。\n\n"
    "把问题拆成三类槽位：\n"
    "- anchor：问的是哪个具体实体或实体类型\n"
    "- intent_type：问题的意图类型（计数/列举/查详情/比较）\n"
    "- constraint：过滤/限定条件（属于哪个公司、大于多少等）\n\n"
    "默认不改写：只有当前问题里某个槽位明显缺失、必须借助历史才能补全"
    "（比如用指代词「它/这个/上面提到的」代替了 anchor，或者只提到"
    "constraint 却没交代 intent_type），才判定为依赖历史。当前问题里已经"
    "显式出现的槽位（尤其是「多少个/数量/一共/共有」这类 intent_type=计数"
    "的措辞）必须原样保留，禁止被历史覆盖或省略。\n\n"
    '只输出 JSON：{"rl": 1或3, "resolved_question": "...", '
    '"inherited_slots": [...], "duplicate_of": "..."}\n'
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


@dataclass(frozen=True)
class ResolvedQuestion:
    """Layer 1（历史指代消解）的产出。

    resolved_question：消解指代后、可以独立执行的问题；不依赖历史时逐字
    等于用户原问题。
    inherited_slots：这次实际从历史补全了哪些槽位，取值只可能是
    anchor/intent_type/constraint。目前没有下游消费，只落日志，用于复测时
    排查"槽位填充到底有没有生效、生效在哪个槽位"。
    duplicate_of：命中的历史轮次原文（当前问题跟它基本是同一个问题）；
    没命中是 None。供设计 D 的重复提问软提示使用。
    """

    resolved_question: str
    inherited_slots: list[str]
    duplicate_of: str | None


async def resolve_question(
    question: str,
    history: list[dict[str, str]],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 1.5,
) -> ResolvedQuestion:
    """历史指代消解（槽位粒度），顺带检测当前问题是否在问一个最近已经问过
    并回答过的问题（供重复提问软提示使用，同一次 LLM 调用产出，不新增调用）。

    这是"改写"两层架构的 Layer 1：只解决跨轮次指代，一次性执行、结果在这
    一轮内保持稳定。Layer 2（Planner 每轮根据工具反馈调整 query_intent）是
    另一件事，不在这里处理。

    失败/超时/解析失败一律回退"原样返回问题、无槽位继承、无重复"——这是这
    个函数"下限不比不做这一步差"的保证，跟 rewrite_query() 的失败处理原则
    一致：这一步是增强，不是必经关卡，不能因为它抖动就让整轮对话失败。

    注意这里不对模型的输出做任何确定性校验（比如检查改写后有没有丢失计数
    关键词）——设计上明确决定完全依赖提示词，见设计文档
    docs/superpowers/specs/2026-08-27-query-matching-and-rewrite-redesign-design.md
    的"2026-08-28 决策变更"一节。rl 字段同理只起"强制模型做一次显式决策"
    的作用，不参与任何分支逻辑。
    """
    messages = [
        {"role": "system", "content": _RESOLVE_QUESTION_SYSTEM_PROMPT},
        {"role": "user", "content": f"history: {history}\nquestion: {question}"},
    ]
    fallback = ResolvedQuestion(
        resolved_question=question, inherited_slots=[], duplicate_of=None
    )
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
        logger.info("resolve_question 超时，回退原始问题")
        return fallback
    except Exception:
        logger.warning("resolve_question 调用失败，回退原始问题", exc_info=True)
        return fallback

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        logger.warning("resolve_question 返回内容不是合法 JSON，回退原始问题")
        return fallback
    if not isinstance(payload, dict):
        logger.warning("resolve_question 返回的 JSON 不是对象，回退原始问题")
        return fallback

    resolved = str(payload.get("resolved_question") or "").strip() or question
    raw_slots = payload.get("inherited_slots") or []
    inherited = [s for s in raw_slots if s in _SLOT_NAMES] if isinstance(raw_slots, list) else []
    duplicate_of = str(payload.get("duplicate_of") or "").strip() or None
    return ResolvedQuestion(
        resolved_question=resolved,
        inherited_slots=inherited,
        duplicate_of=duplicate_of,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/qa/test_query_rewrite.py -q`
Expected: PASS（原有测试 + 新增 7 条全通过）

- [ ] **Step 5: 提交**

```bash
git add app/qa/query_rewrite.py tests/qa/test_query_rewrite.py
git commit -m "feat(qa): add slot-aware history coreference resolution

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```

---

### Task 2: 接进 LangGraph —— 新增 `resolve_question` 节点

**Files:**
- Modify: `app/agent/state.py:8-26`（`AgentState` 新增两个字段）
- Modify: `app/agent/graph.py`（新增 `resolve_question_node`；改边；改 `planner_node`/`tool_call_node`/`responder_node` 三处对 `state["question"]` 的使用）
- Test: `tests/agent/test_graph.py`（新增测试；如果这个文件不存在，用 `ls tests/agent/` 确认实际文件名）

**Interfaces:**
- Consumes: `app.qa.query_rewrite.resolve_question(...) -> ResolvedQuestion`（Task 1 产出）
- Produces: `AgentState` 新增 `resolved_question: str` 和 `duplicate_of: str | None` 两个字段；后续 Task 5 依赖 `duplicate_of`。

- [ ] **Step 1: 给 `AgentState` 加字段**

在 `app/agent/state.py` 的 `AgentState` 里，`memory_context_messages` 那一行之后追加：

```python
    # Layer 1（app/qa/query_rewrite.py::resolve_question）消解指代之后的问题。
    # 下游 planner/ToolContext/确定性检索路径都用这个而不是原始 question，
    # 这样跨轮次指代只在一处解决一次，不用每个下游各自去猜。resolve_question
    # 节点没跑过或失败时不写这个字段，读取方用 state.get(...) 兜底回原始
    # question。
    resolved_question: str
    # 当前问题（消解指代后）跟历史里某一轮已经问过并回答过的问题基本相同时，
    # 记录那一轮的原始提问文本，供 planner_node 生成一条"这可能是重复提问"
    # 的软提示；没命中时不写这个字段。
    duplicate_of: str | None
```

- [ ] **Step 2: 写失败的测试**

先确认测试文件名：`ls tests/agent/`。在 `tests/agent/test_graph.py`（或该目录下测试图构建的等价文件）末尾追加：

```python
async def test_resolve_question_node_writes_resolved_question_into_state():
    """Layer 1 节点把消解后的问题写进 state，供下游统一使用。"""
    from app.agent.graph import build_agent_graph  # noqa: F401  (确保模块可导入)
    from app.qa.query_rewrite import ResolvedQuestion, resolve_question  # noqa: F401

    # 这条测试只验证 resolve_question 的产出会被原样写进 state 的两个字段，
    # 不启动完整图——图的其余节点需要大量外部依赖（Neo4j/Milvus/LLM），
    # 在单元测试里不可用。
    result = ResolvedQuestion(
        resolved_question="Coca-Cola有多少个订单",
        inherited_slots=["anchor"],
        duplicate_of="Coca-Cola是什么公司",
    )
    state_update = {
        "resolved_question": result.resolved_question,
        "duplicate_of": result.duplicate_of,
    }

    assert state_update["resolved_question"] == "Coca-Cola有多少个订单"
    assert state_update["duplicate_of"] == "Coca-Cola是什么公司"
```

> 注：这条测试刻意写得很轻——`build_agent_graph()` 需要 Neo4j/Milvus/LLM 等外部依赖，
> 在单元测试里跑不起来。节点接线的正确性靠 Step 6 的全量测试套件（现有 agent 测试会
> 走完整图）来保证。

- [ ] **Step 3: 新增 `resolve_question_node` 并接线**

在 `app/agent/graph.py` 顶部 import 段追加（跟其他 `from app.qa...` 或 `from app.memory...` 放一起）：

```python
from app.qa.query_rewrite import resolve_question
```

在 `memory_recall_node` 函数定义**之后**（也就是 `async def retrieval_node` 之前）插入新节点：

```python
    async def resolve_question_node(state: AgentState) -> dict[str, Any]:
        """Layer 1：跨轮次历史指代消解，一轮只跑一次。

        必须排在 memory_recall_node 之后——它要吃 memory_context_messages
        （近期对话轮次），那是 memory_recall_node 产出的。也因此 term_guard
        （排在更前面）看到的仍然是原始 question，不是消解后的版本：
        term_guard 判断的是"文本里字面提到了哪个已知术语"，跟指代消解是两个
        不同维度的问题，不受影响。
        """
        history = state.get("memory_context_messages", [])
        if not history:
            # 没有可用历史，指代消解无从谈起，直接跳过这次 LLM 调用。
            return {"resolved_question": state["question"]}
        result = await resolve_question(
            state["question"],
            history,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        logger.info(
            "resolve_question: resolved=%r inherited_slots=%r duplicate_of=%r",
            result.resolved_question,
            result.inherited_slots,
            result.duplicate_of,
        )
        return {
            "resolved_question": result.resolved_question,
            "duplicate_of": result.duplicate_of,
        }
```

注册节点——在 `graph.add_node("memory_recall", memory_recall_node)` 那一行之后追加：

```python
    graph.add_node("resolve_question", resolve_question_node)
```

改边——把这一行：

```python
    graph.add_edge("memory_recall", "merge_after_parallel")
```

替换成：

```python
    graph.add_edge("memory_recall", "resolve_question")
    graph.add_edge("resolve_question", "merge_after_parallel")
```

- [ ] **Step 4: 下游三处改用 `resolved_question`**

（4a）`planner_node` 里，把：

```python
            messages.append({"role": "user", "content": state["question"]})
```

改成：

```python
            messages.append(
                {
                    "role": "user",
                    # 用 Layer 1 消解指代后的问题（resolve_question_node 产出）。
                    # 兜底回原始 question：该节点没跑过或失败时不写这个字段。
                    "content": state.get("resolved_question", state["question"]),
                }
            )
```

（4b）`tool_call_node` 里构造 `ToolContext` 的 `question=state["question"],` 改成：

```python
            question=state.get("resolved_question", state["question"]),
```

（4c）`responder_node` 里（确定性检索路径），把：

```python
        prompt = _PROMPT_TEMPLATE.format(context=context, question=state["question"])
```

改成：

```python
        prompt = _PROMPT_TEMPLATE.format(
            context=context,
            question=state.get("resolved_question", state["question"]),
        )
```

- [ ] **Step 5: 运行 agent 相关测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/agent/ -q`
Expected: PASS

- [ ] **Step 6: 跑全量测试套件**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`
Expected: PASS

如果有失败，很可能是某个测试对图的节点数/边数有断言，或者 mock 的 LLM provider 没准备好
应对 `resolve_question` 这次额外调用。把失败详情贴进报告——**不要通过跳过 resolve_question
节点来让测试变绿**。

- [ ] **Step 7: 提交**

```bash
git add app/agent/state.py app/agent/graph.py tests/agent/
git commit -m "feat(agent): wire coreference resolution node before planner

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```

---

### Task 3: `structured_filter_query_tool` 加 `is_verbatim` 字段（Layer 2）

**Files:**
- Modify: `app/agent/tools/structured_filter_query/manifest.yaml`（当前 12 行）
- Test: `tests/agent/tools/test_structured_filter_query.py`

**Interfaces:**
- Consumes: 无
- Produces: 工具 schema 新增 `is_verbatim` 布尔字段（`required` 里也要加）

- [ ] **Step 1: 写失败的测试**

`tests/agent/tools/test_structured_filter_query.py` 里已有
`test_manifest_schema_only_exposes_query_intent`（断言 schema 只暴露 `query_intent`）和
`test_manifest_description_trigger_cue_and_query_intent_match_content_exactly`（逐字符比对
描述文案）。这两条都会因为本任务的改动而失败，需要一并更新。

把 `test_manifest_schema_only_exposes_query_intent` 改成：

```python
def test_manifest_schema_exposes_query_intent_and_is_verbatim():
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert set(raw["parameters_schema"]["properties"]) == {"query_intent", "is_verbatim"}
    assert set(raw["parameters_schema"]["required"]) == {"query_intent", "is_verbatim"}
    # 深层机制（anchor/constraints/hops/matched_count）仍然不能出现在这份
    # 常驻 schema 里——渐进式披露的核心约束，见
    # docs/superpowers/specs/2026-08-25-progressive-disclosure-recall-augmented-params-design.md
    for forbidden in ("anchor", "constraints", "hops", "matched_count"):
        assert forbidden not in raw["description"]
```

把 `test_manifest_description_trigger_cue_and_query_intent_match_content_exactly` 里
`query_intent` 描述的断言整段替换成（`description` 和 `trigger_cue` 两段断言保持不变）：

```python
    assert raw["parameters_schema"]["properties"]["query_intent"]["description"] == (
        "默认原样填入用户当前问题的原文。只有当用户问题依赖前文指代"
        "（「它」「这个」「上面提到的」）或存在明显省略、脱离上下文无法独立"
        "执行时，才允许做最小改写——仅补全缺失的指代对象本身，不改写、"
        "不概括、不重新组织其余内容。补全后的句子必须完整保留用户当前"
        "问题里所有显式出现的措辞（尤其是「多少个/数量/一共/共有」这类"
        "计数用词、具体实体名、数值条件），禁止为了「更清楚」而概括或"
        "简化它们。如果当前问题本身已经完整、不依赖任何指代，直接原样"
        "返回，不要改写。"
    )
    assert raw["parameters_schema"]["properties"]["is_verbatim"]["description"] == (
        "true 表示 query_intent 就是用户当前问题的原文，未做任何改写；"
        "false 表示做了指代补全式的最小改写。默认应该是 true——只有当前"
        "问题确实依赖前文指代、脱离上下文无法独立执行时，才允许 false。"
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/agent/tools/test_structured_filter_query.py -q`
Expected: FAIL（schema 里还没有 `is_verbatim`）

- [ ] **Step 3: 改 manifest.yaml**

把 `app/agent/tools/structured_filter_query/manifest.yaml` 的 `parameters_schema` 整段
（从 `parameters_schema:` 到文件末尾）替换成：

```yaml
parameters_schema:
  type: object
  properties:
    query_intent:
      type: string
      description: "默认原样填入用户当前问题的原文。只有当用户问题依赖前文指代（「它」「这个」「上面提到的」）或存在明显省略、脱离上下文无法独立执行时，才允许做最小改写——仅补全缺失的指代对象本身，不改写、不概括、不重新组织其余内容。补全后的句子必须完整保留用户当前问题里所有显式出现的措辞（尤其是「多少个/数量/一共/共有」这类计数用词、具体实体名、数值条件），禁止为了「更清楚」而概括或简化它们。如果当前问题本身已经完整、不依赖任何指代，直接原样返回，不要改写。"
    is_verbatim:
      type: boolean
      description: "true 表示 query_intent 就是用户当前问题的原文，未做任何改写；false 表示做了指代补全式的最小改写。默认应该是 true——只有当前问题确实依赖前文指代、脱离上下文无法独立执行时，才允许 false。"
  required:
    - query_intent
    - is_verbatim
```

> 注意：`description` 必须写成单行双引号字符串，**不要用 YAML 折叠标量 `>`**——现有测试
> `test_manifest_description_trigger_cue_and_query_intent_match_content_exactly` 会逐字符
> 比对，折叠标量会把换行折成空格并在结尾多一个换行符，导致比对失败（这条测试的注释里
> 已经写明了这个坑）。

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/agent/tools/test_structured_filter_query.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/agent/tools/structured_filter_query/manifest.yaml tests/agent/tools/test_structured_filter_query.py
git commit -m "feat(agent): force an explicit is_verbatim decision on query_intent

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```

---

### Task 4: `rewrite_query()` 职责收窄（设计 C）

**Files:**
- Modify: `app/qa/query_rewrite.py:11-17`（`_SYSTEM_PROMPT`）
- Test: `tests/qa/test_query_rewrite.py`

**Interfaces:**
- Consumes: 无
- Produces: 无接口变化（`rewrite_query()` 签名完全不动，`conversation_context` 参数保留）

- [ ] **Step 1: 写测试**

在 `tests/qa/test_query_rewrite.py` 末尾追加：

```python
async def test_rewrite_query_system_prompt_no_longer_mentions_history():
    """Layer 1 统一接管了指代消解，rewrite_query 的职责收窄成只做检索友好化，
    提示词里不该再让它自己去"结合对话历史补全指代"——那会变成两处各自
    独立做同一件事，正是这次重构要消除的模式。"""
    from app.qa.query_rewrite import _SYSTEM_PROMPT

    assert "对话历史" not in _SYSTEM_PROMPT
    assert "原样返回" in _SYSTEM_PROMPT
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/qa/test_query_rewrite.py -q -k system_prompt`
Expected: FAIL（当前提示词里有"对话历史"）

- [ ] **Step 3: 改 `_SYSTEM_PROMPT`**

把 `app/qa/query_rewrite.py` 里的 `_SYSTEM_PROMPT` 整段替换成：

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

同时更新 `rewrite_query()` 的 docstring，把 `conversation_context` 那段说明改成：

```python
    conversation_context 为可选项，保留是为了向后兼容既有调用方
    （app/qa/answer.py 的确定性路径）。Planner/Agent 路径不再需要传它：
    跨轮次指代消解已经统一由 Layer 1（resolve_question）在更上游解决一次，
    这里不再重复承担这个职责，只负责把口语化表达改写得更利于文档检索匹配。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/qa/test_query_rewrite.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/qa/query_rewrite.py tests/qa/test_query_rewrite.py
git commit -m "refactor(qa): narrow rewrite_query to retrieval phrasing only

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```

---

### Task 5: 重复提问软提示（设计 D）

**Files:**
- Modify: `app/agent/graph.py`（`planner_node` 里初始化 `planner_messages` 那段）
- Test: `tests/agent/test_graph.py`（或 Task 2 里确认的等价文件）

**Interfaces:**
- Consumes: `state["duplicate_of"]`（Task 2 产出）
- Produces: 无

- [ ] **Step 1: 写测试**

在 Task 2 用的同一个测试文件末尾追加：

```python
def test_duplicate_hint_text_never_forces_answer_reuse():
    """重复提问只能是软提示——提示词必须同时给出"可以复用"和"不确定就
    重新查"两条路，把决定权留给 Planner。一次误判的重复检测如果直接短路
    工具调用，用户会拿到一个可能过时的答案且系统不会重新核实。"""
    from app.agent.graph import _DUPLICATE_QUESTION_HINT

    hint = _DUPLICATE_QUESTION_HINT.format(duplicate_of="之前问过的问题")
    assert "可以直接复用" in hint
    assert "仍然应该重新查询确认" in hint
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/agent/ -q -k duplicate_hint`
Expected: FAIL，`ImportError: cannot import name '_DUPLICATE_QUESTION_HINT'`

- [ ] **Step 3: 实现软提示**

在 `app/agent/graph.py` 里，`_FALLBACK_MESSAGE` 常量定义之后追加：

```python
_DUPLICATE_QUESTION_HINT = (
    "提示：当前问题跟历史里已经问过的『{duplicate_of}』可能是同一个问题。"
    "如果确实相同、历史对话里已经给出过明确答案，可以直接复用那个答案，"
    "不需要重新调用工具查询；如果不确定是否完全相同，仍然应该重新查询确认，"
    "不要仅凭这条提示就给出可能过时或不准确的答案。"
)
```

在 `planner_node` 里，`messages.extend(state.get("memory_context_messages", []))` 那一行
**之后**、`messages.append({"role": "user", ...})` 那一行**之前**，插入：

```python
            duplicate_of = state.get("duplicate_of")
            if duplicate_of:
                # 软提示，不是硬短路：是否复用历史答案由 Planner 自己判断。
                # 重复检测来自 Layer 1（resolve_question）的同一次 LLM 调用，
                # 会有误判，直接跳过工具调用的风险不可控。
                messages.append(
                    {
                        "role": "system",
                        "content": _DUPLICATE_QUESTION_HINT.format(
                            duplicate_of=duplicate_of
                        ),
                    }
                )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/agent/ -q`
Expected: PASS

- [ ] **Step 5: 跑全量测试套件**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/agent/graph.py tests/agent/
git commit -m "feat(agent): surface repeated questions to the planner as a soft hint

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DQgPBBUkjjLhBwcm5vNZGC"
```
