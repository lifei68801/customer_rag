# 记忆模块剩余架构缺口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `docs/superpowers/specs/2026-08-06-memory-module-gaps-design.md` 里的 4 项独立设计：即时纠错通道、P1 结构化历史检索、主动跟进新增两种触发（已知修复告知 + 稍后再试确认）、Redis 可插拔会话滑窗后端。

**Architecture:** 5 个阶段，按 spec 建议的依赖顺序排列，每个阶段产出可独立测试、可独立提交的成果；阶段之间除阶段5复用阶段无直接代码依赖，可以任意顺序执行或跳过。

**Tech Stack:** Python 3.12 / aiosqlite / pytest（pytest-asyncio auto 模式）/ LangGraph（阶段1路由改动）/ redis-py（阶段5新依赖，可选）。

## Global Constraints

- 每个新模块/每个新增能力点都必须先写失败测试（RED）→ 确认失败 → 最小实现（GREEN）→ 确认通过 → 跑一次全量测试套件（`pytest tests/ -q`，Windows 下用 `.venv/Scripts/python.exe -m pytest tests/ -q`）确认无回归 → git commit。这是本仓库贯穿全程的强制流程，不是可选项。
- LLM 相关的新函数一律遵循"LLM 优先 + 规则/确定性兜底"：`asyncio.wait_for` 超时保护 + `except Exception` 广泛捕获，失败/超时/JSON解析失败都要有明确的降级行为，不能让异常直接抛出中断主流程（除非 spec 里明确说了这个操作应该失败可见，例如 `register_known_fix` 的 embedding 失败）。
- 新增的可选参数默认值必须保证"不传参数时行为与当前完全一致"——这是本仓库所有记忆模块功能的既定约束（`memory_conn=None`/`ticket_conn=None`/`min_relevance_score=None` 等都是这个模式）。
- 每个新 SQLite 表都有自己独立的 `ensure_*_schema(conn)` 函数（不并入 `app/memory/schema.py` 的核心 `ensure_schema`），调用方显式调用——这是本仓库 Task18 之后新增表的既定风格。
- 提交信息格式：一行摘要（`feat:`/`fix:` 前缀）+ 空行 + 详细说明为什么这么做、复用了什么、明确不做什么，中文书写，参照本仓库既有提交历史的写法。
- Windows 环境下运行测试统一使用 `.venv/Scripts/python.exe -m pytest`，不要用系统 `python`/`pytest` 命令（本仓库这台机器上没有全局安装 pytest）。

---

## 阶段 1：即时纠错通道

### Task 1：纠错意图检测

**Files:**
- Create: `app/memory/correction_intent.py`
- Test: `tests/memory/test_correction_intent.py`

**Interfaces:**
- Produces: `async def detect_correction_intent(text: str, *, llm_registry: ProviderRegistry, llm_provider_name: str, timeout_sec: float = 2.0) -> bool`

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_correction_intent.py`：

```python
from app.memory.correction_intent import detect_correction_intent
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider 挂了")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_detects_correction_when_llm_says_true():
    result = await detect_correction_intent(
        "你记错了，其实我用的是macOS",
        llm_registry=_registry(FixedLLMProvider('{"is_correction": true}')),
        llm_provider_name="llm",
    )
    assert result is True


async def test_does_not_detect_correction_for_normal_question():
    result = await detect_correction_intent(
        "网络连不上怎么办",
        llm_registry=_registry(FixedLLMProvider('{"is_correction": false}')),
        llm_provider_name="llm",
    )
    assert result is False


async def test_falls_back_to_rule_when_llm_fails():
    result = await detect_correction_intent(
        "你记错了",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
    )
    assert result is True


async def test_rule_fallback_does_not_flag_normal_question_when_llm_fails():
    result = await detect_correction_intent(
        "网络连不上怎么办",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
    )
    assert result is False


async def test_falls_back_to_rule_when_llm_returns_invalid_json():
    result = await detect_correction_intent(
        "弄错了，应该是别的配置",
        llm_registry=_registry(FixedLLMProvider("不是合法JSON")),
        llm_provider_name="llm",
    )
    assert result is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_correction_intent.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.correction_intent'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/correction_intent.py`：

```python
from __future__ import annotations

import asyncio
import json
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是客服对话意图分类器。判断用户这句话是否在纠正你之前记错的信息"
    "（例如“你记错了”“其实不是这样”“应该是……不是……”）。"
    '只输出 JSON：{"is_correction": true/false}。'
)

_CORRECTION_KEYWORDS = ("记错了", "弄错了", "不对，应该是", "更正一下", "搞错了")


def _looks_like_correction_by_rule(text: str) -> bool:
    return any(keyword in text for keyword in _CORRECTION_KEYWORDS)


async def detect_correction_intent(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> bool:
    """判断这句话是不是在纠正之前记错的信息；LLM 失败/超时/解析失败时
    降级为关键词规则兜底，规则命中即判 True——宁可多触发一次短路由走
    完整决策链路确认，也不要漏判导致纠正没生效。
    """
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("纠错意图检测超时，回退规则判断")
        return _looks_like_correction_by_rule(text)
    except Exception:
        logger.warning("纠错意图检测失败，回退规则判断", exc_info=True)
        return _looks_like_correction_by_rule(text)

    try:
        payload = json.loads(result.text)
        return bool(payload.get("is_correction", False))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return _looks_like_correction_by_rule(text)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_correction_intent.py -v`
Expected: 5 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过，无回归

- [ ] **Step 6: 提交**

```bash
git add app/memory/correction_intent.py tests/memory/test_correction_intent.py
git commit -m "feat: add correction-intent detection for immediate memory fix channel"
```

---

### Task 2：`correction_check_node` 接入图

**Files:**
- Modify: `app/agent/state.py`
- Modify: `app/agent/graph.py`
- Test: `tests/agent/test_graph.py`

**Interfaces:**
- Consumes: `detect_correction_intent(text, *, llm_registry, llm_provider_name, timeout_sec=2.0) -> bool`（Task 1）；`find_similar_memory_items(conn, *, tenant_id, user_id, query_vector, top_k) -> list[dict]`（已有，`app/memory/similarity.py`）；`extract_facts(*, user_input, assistant_output, llm_registry, llm_provider_name, timeout_sec=2.0) -> list[str]`（已有，`app/memory/fact_extractor.py`）；`resolve_memory_actions(*, new_facts, existing_memories, llm_registry, llm_provider_name, timeout_sec=2.0) -> list[dict[str,str]]`（已有，`app/memory/conflict_resolver.py`，返回项含 `event`/`memory_id`/`text`/`reason`）；`apply_memory_actions(conn, *, tenant_id, user_id, actions, embedding_registry=None, embedding_provider_name=None) -> list[dict[str,str]]`（已有，`app/memory/action_executor.py`，返回项含 `event`/`memory_id`/`text`）
- Produces: `AgentState.is_correction_handled: bool`；`build_agent_graph()` 图新增 `correction_check` 节点，位于 `input_safety` 之后、`clarification_check` 之前

- [ ] **Step 1: 写失败测试**

在 `tests/agent/test_graph.py` 末尾追加（复用文件顶部已有的 `_build_dependencies`/`FakeEmbeddingProvider`/`ProviderRegistry`/`ProviderCapability`/`ProviderRequest`/`ProviderResult` 导入，无需新增 import）：

```python
async def test_correction_check_short_circuits_and_updates_memory():
    import aiosqlite

    from app.memory.memory_store import list_active_memory_items, upsert_memory_item
    from app.memory.schema import ensure_schema

    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await upsert_memory_item(
        conn,
        memory_id="m1",
        tenant_id="t1",
        user_id="u1",
        text="客户使用Windows系统",
        embedding=[1.0, 0.0],
    )

    embedding_registry, vector_store, bm25_index, _unused_registry, _unused_provider = (
        await _build_dependencies(with_records=True, llm_text="不应该被用到")
    )

    class ScriptedLLMProvider:
        def __init__(self, responses: list[str]) -> None:
            self._responses = list(responses)
            self.requests: list[ProviderRequest] = []

        async def complete(self, request: ProviderRequest) -> ProviderResult:
            self.requests.append(request)
            return ProviderResult(text=self._responses.pop(0))

    llm_provider = ScriptedLLMProvider(
        [
            '{"is_correction": true}',  # correction_check_node 的意图检测
            '{"facts": ["客户使用macOS系统"]}',  # fact_extractor
            '{"actions": [{"event": "UPDATE", "target_memory_id": "m1", '
            '"text": "客户使用macOS系统", "reason": "客户更正"}]}',  # conflict_resolver
        ]
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        memory_conn=conn,
    )

    result = await graph.ainvoke(
        {
            "question": "你记错了，其实我用的是macOS系统",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    assert result["is_correction_handled"] is True
    assert "macOS系统" in result["final_text"]
    assert len(llm_provider.requests) == 3  # 意图检测+事实抽取+冲突决策，没有额外的检索/responder调用

    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    updated = next(item for item in items if item["memory_id"] == "m1")
    assert updated["text"] == "客户使用macOS系统"


async def test_correction_check_does_not_trigger_for_normal_question():
    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(with_records=True, llm_text="重启路由器即可解决。")
    )
    import aiosqlite

    from app.memory.schema import ensure_schema

    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        memory_conn=conn,
    )

    result = await graph.ainvoke(
        {
            "question": "网络连不上怎么办？",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    assert result.get("is_correction_handled") is not True
    assert result["final_text"] == "重启路由器即可解决。"
```

注意：`test_correction_check_does_not_trigger_for_normal_question` 里 `_build_dependencies(with_records=True, llm_text="重启路由器即可解决。")` 返回的 `llm_registry` 用的是 `FakeLLMProvider`（只有一个固定 `_text`，不管调用几次都返回同一个文本）——`detect_correction_intent` 对这个固定文本做 `json.loads` 会失败（不是合法JSON），触发规则兜底，而"网络连不上怎么办？"不含任何纠错关键词，规则判 False，符合测试预期。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph.py -v -k correction_check`
Expected: `AssertionError` 或 `KeyError: 'is_correction_handled'`（因为 `correction_check_node` 还不存在，`is_correction_handled` 字段不会出现在结果里）

- [ ] **Step 3: 写最小实现**

修改 `app/agent/state.py`，在 `needs_clarification: bool` 后新增一行：

```python
    is_correction_handled: bool
```

修改 `app/agent/graph.py`：

1. 顶部新增 import（`from app.memory.correction_intent import detect_correction_intent` 加在 `from app.memory.consolidation_queue import enqueue_consolidation_job` 之前一行，保持字母序不强求，跟随现有顺序即可）：

```python
from app.memory.action_executor import apply_memory_actions
from app.memory.conflict_resolver import resolve_memory_actions
from app.memory.correction_intent import detect_correction_intent
from app.memory.fact_extractor import extract_facts
from app.memory.similarity import find_similar_memory_items
```

2. 在 `input_safety_node` 定义之后、`clarification_check_node` 定义之前，插入新节点：

```python
    async def correction_check_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is None:
            return {}
        question = state["question"]
        is_correction = await detect_correction_intent(
            question, llm_registry=llm_registry, llm_provider_name=llm_provider_name
        )
        if not is_correction:
            return {}

        tenant_id = state["tenant_id"]
        user_id = state.get("user_id", "")
        facts = await extract_facts(
            user_input=question,
            assistant_output="",
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        if not facts:
            return {}

        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=facts), provider_name=embedding_provider_name
        )
        candidates: dict[str, dict[str, Any]] = {}
        for vector in embed_result.vectors:
            similar = await find_similar_memory_items(
                memory_conn,
                tenant_id=tenant_id,
                user_id=user_id,
                query_vector=vector,
                top_k=20,
            )
            for item in similar:
                candidates[item["memory_id"]] = item

        actions = await resolve_memory_actions(
            new_facts=facts,
            existing_memories=list(candidates.values()),
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        applied = await apply_memory_actions(
            memory_conn,
            tenant_id=tenant_id,
            user_id=user_id,
            actions=actions,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
        )
        if not applied:
            return {}

        primary = applied[0]
        if primary["event"] in ("UPDATE", "DELETE"):
            confirmation = f"好的，已经帮您更正为：{primary['text']}" if primary["text"] else "好的，已经帮您更正了。"
        else:
            confirmation = f"好的，已经记下：{primary['text']}"
        return {
            "is_correction_handled": True,
            "fallback_triggered": False,
            "final_text": confirmation,
        }
```

（`EmbeddingRequest` 需要 import：在文件顶部 `from app.providers.embedding import EmbeddingRegistry` 这一行改成 `from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest`）

3. 修改路由：`route_after_input_safety` 的目标从 `clarification_check` 改为 `correction_check`：

```python
    def route_after_input_safety(state: AgentState) -> str:
        return (
            "correction_check" if state.get("is_input_safe", True) else "output_safety"
        )

    def route_after_correction_check(state: AgentState) -> str:
        return "output_safety" if state.get("is_correction_handled") else "clarification_check"
```

4. 图注册部分，`graph.add_node("clarification_check", clarification_check_node)` 之前新增：

```python
    graph.add_node("correction_check", correction_check_node)
```

5. 边的注册，把：

```python
    graph.add_conditional_edges(
        "input_safety",
        route_after_input_safety,
        {"clarification_check": "clarification_check", "output_safety": "output_safety"},
    )
    graph.add_edge("clarification_check", "term_guard")
```

改成：

```python
    graph.add_conditional_edges(
        "input_safety",
        route_after_input_safety,
        {"correction_check": "correction_check", "output_safety": "output_safety"},
    )
    graph.add_conditional_edges(
        "correction_check",
        route_after_correction_check,
        {"output_safety": "output_safety", "clarification_check": "clarification_check"},
    )
    graph.add_edge("clarification_check", "term_guard")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph.py -v`
Expected: 全部通过（含新增的 2 条 + 原有全部用例）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过，无回归

- [ ] **Step 6: 提交**

```bash
git add app/agent/state.py app/agent/graph.py tests/agent/test_graph.py
git commit -m "feat: wire immediate correction channel into agent graph"
```

---

## 阶段 2：P1 结构化历史检索

### Task 3：按时间窗口查询对话轮次

**Files:**
- Create: `app/memory/structured_recall.py`
- Test: `tests/memory/test_structured_recall.py`

**Interfaces:**
- Produces: `async def query_turns_in_window(conn, *, tenant_id: str, user_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]`（跨 `session_id`）

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_structured_recall.py`：

```python
from datetime import datetime

import aiosqlite

from app.memory.schema import ensure_schema
from app.memory.structured_recall import query_turns_in_window


async def _insert_turn(conn, *, tenant_id, session_id, user_id, role, content, created_at):
    await conn.execute(
        "INSERT INTO conversation_turns (tenant_id, session_id, user_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, session_id, user_id, role, content, created_at),
    )
    await conn.commit()


async def test_finds_turns_within_window_across_sessions():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user",
        content="上周的问题A", created_at="2026-07-28 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s2", user_id="u1", role="user",
        content="另一个会话里上周的问题B", created_at="2026-07-29 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user",
        content="太早之前的问题", created_at="2026-07-01 10:00:00",
    )

    results = await query_turns_in_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
    )

    contents = {row["content"] for row in results}
    assert contents == {"上周的问题A", "另一个会话里上周的问题B"}


async def test_excludes_other_tenant_and_other_user():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await _insert_turn(
        conn, tenant_id="t2", session_id="s1", user_id="u1", role="user",
        content="别的租户的问题", created_at="2026-07-28 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u2", role="user",
        content="别的用户的问题", created_at="2026-07-28 10:00:00",
    )

    results = await query_turns_in_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
    )

    assert results == []


async def test_returns_empty_list_when_nothing_in_window():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    results = await query_turns_in_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
    )

    assert results == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_structured_recall.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.structured_recall'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/structured_recall.py`：

```python
from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite

_SQLITE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


async def query_turns_in_window(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """按客户ID+时间窗口查询原始对话轮次，跨 session_id（"上周的会话"
    大概率不是当前 session）。architecture doc §6.3 P1 结构化历史检索。

    created_at 比较用字符串格式（和 conversation_turns 表 created_at
    列的 SQLite `datetime('now')` 默认值格式一致：'YYYY-MM-DD HH:MM:SS'），
    不解析成 datetime 对象再比较——避免引入时区/精度不一致的转换风险。
    """
    conn.row_factory = aiosqlite.Row
    start_str = start.strftime(_SQLITE_DATETIME_FORMAT)
    end_str = end.strftime(_SQLITE_DATETIME_FORMAT)
    cursor = await conn.execute(
        "SELECT session_id, role, content, created_at FROM conversation_turns "
        "WHERE tenant_id = ? AND user_id = ? AND created_at BETWEEN ? AND ? "
        "ORDER BY created_at",
        (tenant_id, user_id, start_str, end_str),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_structured_recall.py -v`
Expected: 3 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/structured_recall.py tests/memory/test_structured_recall.py
git commit -m "feat: query conversation turns by customer id and time window"
```

---

### Task 4：窗口内关键词二次过滤

**Files:**
- Modify: `app/memory/structured_recall.py`
- Test: `tests/memory/test_structured_recall.py`

**Interfaces:**
- Consumes: `query_turns_in_window(...)`（Task 3）；`BM25Index`/`BM25Hit`（已有，`app/retrieval/bm25.py`）；`VectorRecord`（已有，`app/retrieval/vector_store.py`）
- Produces: `async def search_turns_by_keyword_and_window(conn, *, tenant_id: str, user_id: str, start: datetime, end: datetime, question: str, top_k: int = 5) -> list[dict[str, Any]]`

- [ ] **Step 1: 写失败测试**

在 `tests/memory/test_structured_recall.py` 末尾追加：

```python
from app.memory.structured_recall import search_turns_by_keyword_and_window


async def test_keyword_filter_narrows_down_window_results():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await _insert_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user",
        content="错误码E502网关超时怎么解决", created_at="2026-07-28 10:00:00",
    )
    await _insert_turn(
        conn, tenant_id="t1", session_id="s2", user_id="u1", role="user",
        content="账号密码忘记了怎么办", created_at="2026-07-29 10:00:00",
    )

    results = await search_turns_by_keyword_and_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
        question="E502网关超时", top_k=5,
    )

    assert len(results) == 1
    assert "E502" in results[0]["content"]


async def test_keyword_filter_returns_empty_when_window_empty():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)

    results = await search_turns_by_keyword_and_window(
        conn, tenant_id="t1", user_id="u1",
        start=datetime(2026, 7, 27), end=datetime(2026, 8, 1),
        question="任意问题", top_k=5,
    )

    assert results == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_structured_recall.py -v -k keyword_filter`
Expected: `ImportError: cannot import name 'search_turns_by_keyword_and_window'`

- [ ] **Step 3: 写最小实现**

在 `app/memory/structured_recall.py` 末尾追加：

```python
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorRecord


async def search_turns_by_keyword_and_window(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    start: datetime,
    end: datetime,
    question: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """在时间窗口内的轮次基础上，再按当前问题做一次关键词过滤——窗口
    可能跨多天多个会话，不加这层过滤会把大量不相关对话也拼进上下文。
    """
    turns = await query_turns_in_window(
        conn, tenant_id=tenant_id, user_id=user_id, start=start, end=end
    )
    if not turns:
        return []

    records = [
        VectorRecord(
            id=str(index), vector=[], text=turn["content"], tenant_id=tenant_id, metadata={}
        )
        for index, turn in enumerate(turns)
    ]
    bm25_index = BM25Index()
    bm25_index.index(records)
    hits = bm25_index.search(question, top_k=top_k, tenant_id=tenant_id)
    hit_indices = [int(hit.id) for hit in hits]
    return [turns[index] for index in hit_indices]
```

（顶部 import 需要加在文件已有的 `import aiosqlite` 之后，与其它 import 放在一起，不要放进函数体内部）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_structured_recall.py -v`
Expected: 5 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/structured_recall.py tests/memory/test_structured_recall.py
git commit -m "feat: add keyword filtering within time-window structured recall"
```

---

### Task 5：接入 `memory_recall_node`

**Files:**
- Modify: `app/agent/graph.py`
- Test: `tests/agent/test_graph_memory.py`

**Interfaces:**
- Consumes: `resolve_time_window(text, *, llm_registry, llm_provider_name, reference_time, min_confidence=0.5, timeout_sec=2.0) -> TimeWindowResult`（已有，`app/memory/temporal_resolver.py`，字段：`resolved`/`start`/`end`/`confidence`/`is_future`/`source`）；`search_turns_by_keyword_and_window(...)`（Task 4）
- Produces: `memory_recall_node` 返回的 `memory_context_messages` 在问题含可解析时间表达式时，额外包含一条独立 system message

- [ ] **Step 1: 写失败测试**

在 `tests/agent/test_graph_memory.py` 末尾追加（复用文件已有的 `_build_dependencies`/imports，若文件里没有 `aiosqlite`/`ensure_schema` 顶层 import，本文件顶部已有，不需要再加）：

```python
async def test_memory_recall_injects_structured_history_for_time_bearing_question():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await append_turn(
        conn, tenant_id="t1", session_id="s0", user_id="u1",
        role="user", content="错误码E502网关超时怎么解决",
    )
    # 手动把 created_at 改到"昨天"，确保能被 resolve_time_window 解析出的
    # "昨天"窗口命中（append_turn 本身用 datetime('now') 默认值，测试运行
    # 时刻就是"今天"，这里直接改成昨天，不依赖具体的墙钟时间）。
    await conn.execute(
        "UPDATE conversation_turns SET created_at = datetime('now', '-1 day') "
        "WHERE session_id = 's0'"
    )
    await conn.commit()

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(
            [
                '{"start": null, "end": null, "confidence": 0}',  # resolve_time_window 的LLM调用故意给低置信度，回退规则引擎判"昨天"
                "重启路由器即可解决。",  # responder
                '{"is_safe": true}',  # OutputSafety 语义审查
            ]
        )
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        memory_conn=conn,
    )

    await graph.ainvoke(
        {
            "question": "昨天那个E502网关超时问题解决了吗",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    assert len(llm_provider.requests) >= 2
    responder_request = llm_provider.requests[1]
    all_content = " ".join(
        m.get("content", "") for m in responder_request.messages
    )
    assert "E502网关超时怎么解决" in all_content
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_memory.py -v -k structured_history`
Expected: `AssertionError`（历史轮次内容不会出现在 responder 的 prompt 里，因为还没接入）

- [ ] **Step 3: 写最小实现**

修改 `app/agent/graph.py` 的 `memory_recall_node`：

```python
    async def memory_recall_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is None:
            return {"memory_context_messages": []}
        messages = await inject_memory_context(
            memory_conn,
            tenant_id=state["tenant_id"],
            session_id=state.get("session_id", ""),
            user_id=state.get("user_id", ""),
            question=state["question"],
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
        )
        time_result = await resolve_time_window(
            state["question"],
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            reference_time=datetime.now(),
        )
        if time_result.resolved and time_result.start and time_result.end:
            structured_turns = await search_turns_by_keyword_and_window(
                memory_conn,
                tenant_id=state["tenant_id"],
                user_id=state.get("user_id", ""),
                start=time_result.start,
                end=time_result.end,
                question=state["question"],
            )
            if structured_turns:
                lines = "\n".join(
                    f"- {turn['role']}: {turn['content']}" for turn in structured_turns
                )
                messages.append(
                    {
                        "role": "system",
                        "content": f"以下是您在相关时间段提到的历史对话：\n{lines}",
                    }
                )
        return {"memory_context_messages": messages}
```

顶部新增 import：

```python
from app.memory.structured_recall import search_turns_by_keyword_and_window
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_memory.py -v`
Expected: 全部通过

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过（`fallback_node` 里已有一次 `resolve_time_window` 调用，`memory_recall_node` 新增第二处调用点，两处独立，互不影响；已有测试脚本响应数量若因此变化需要相应调整——运行后如有失败，按失败信息在对应测试里补齐新增的这一次 LLM 调用的 scripted response）

- [ ] **Step 6: 提交**

```bash
git add app/agent/graph.py tests/agent/test_graph_memory.py
git commit -m "feat: inject structured time-window history into memory recall"
```

---

## 阶段 3：已知故障修复后主动告知

### Task 6：`known_fixes` 表

**Files:**
- Create: `app/memory/known_fixes.py`
- Test: `tests/memory/test_known_fixes.py`

**Interfaces:**
- Produces: `ensure_known_fixes_schema(conn)`；`async def register_known_fix(conn, *, tenant_id: str, description: str, fixed_at: datetime, embedding_registry: EmbeddingRegistry, embedding_provider_name: str) -> str`（返回 `fix_id`）；`async def list_known_fixes(conn, *, tenant_id: str) -> list[dict[str, Any]]`

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_known_fixes.py`：

```python
from datetime import datetime

import aiosqlite

from app.memory.known_fixes import ensure_known_fixes_schema, list_known_fixes, register_known_fix
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


def _registry() -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register("fake-embedding", FakeEmbeddingProvider())
    return registry


async def test_register_known_fix_persists_with_embedding():
    conn = await aiosqlite.connect(":memory:")
    await ensure_known_fixes_schema(conn)

    fix_id = await register_known_fix(
        conn,
        tenant_id="t1",
        description="网关超时问题已修复",
        fixed_at=datetime(2026, 8, 1, 10, 0, 0),
        embedding_registry=_registry(),
        embedding_provider_name="fake-embedding",
    )

    assert fix_id

    fixes = await list_known_fixes(conn, tenant_id="t1")
    assert len(fixes) == 1
    assert fixes[0]["fix_id"] == fix_id
    assert fixes[0]["description"] == "网关超时问题已修复"
    assert fixes[0]["embedding"] == [1.0, 0.0]


async def test_list_known_fixes_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    await ensure_known_fixes_schema(conn)
    await register_known_fix(
        conn, tenant_id="t1", description="修复A", fixed_at=datetime(2026, 8, 1),
        embedding_registry=_registry(), embedding_provider_name="fake-embedding",
    )

    fixes = await list_known_fixes(conn, tenant_id="t2")

    assert fixes == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_known_fixes.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.known_fixes'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/known_fixes.py`：

```python
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import aiosqlite

from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS known_fixes (
    fix_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    description TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    fixed_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_known_fixes_tenant ON known_fixes (tenant_id);
"""


async def ensure_known_fixes_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def register_known_fix(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    description: str,
    fixed_at: datetime,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
) -> str:
    """登记一条已知故障修复，供 scan_and_send_known_fix_followups() 匹配
    历史工单。这是一次性的管理操作（人工/管理员触发），embedding 调用
    失败时让异常直接上抛——操作者需要知道登记失败了，不能静默丢弃。
    """
    embed_result = await embedding_registry.run(
        EmbeddingRequest(texts=[description]), provider_name=embedding_provider_name
    )
    fix_id = str(uuid.uuid4())
    await ensure_known_fixes_schema(conn)
    await conn.execute(
        "INSERT INTO known_fixes (fix_id, tenant_id, description, embedding_json, fixed_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            fix_id,
            tenant_id,
            description,
            json.dumps(embed_result.vectors[0]),
            fixed_at.timestamp(),
            datetime.now().timestamp(),
        ),
    )
    await conn.commit()
    return fix_id


async def list_known_fixes(conn: aiosqlite.Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM known_fixes WHERE tenant_id = ?", (tenant_id,)
    )
    rows = await cursor.fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["embedding"] = json.loads(item.pop("embedding_json"))
        results.append(item)
    return results
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_known_fixes.py -v`
Expected: 2 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/known_fixes.py tests/memory/test_known_fixes.py
git commit -m "feat: add known_fixes registry for proactive fix notifications"
```

---

### Task 7：`ticket_fix_notifications` 去重表

**Files:**
- Create: `app/memory/ticket_fix_notifications.py`
- Test: `tests/memory/test_ticket_fix_notifications.py`

**Interfaces:**
- Produces: `ensure_ticket_fix_notifications_schema(conn)`；`async def is_already_notified(conn, *, ticket_id: str, fix_id: str) -> bool`；`async def mark_notified(conn, *, ticket_id: str, fix_id: str, now: datetime) -> None`

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_ticket_fix_notifications.py`：

```python
from datetime import datetime

import aiosqlite

from app.memory.ticket_fix_notifications import (
    ensure_ticket_fix_notifications_schema,
    is_already_notified,
    mark_notified,
)


async def test_is_already_notified_false_when_never_marked():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ticket_fix_notifications_schema(conn)

    assert await is_already_notified(conn, ticket_id="tk1", fix_id="fx1") is False


async def test_mark_notified_then_is_already_notified_true():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ticket_fix_notifications_schema(conn)

    await mark_notified(conn, ticket_id="tk1", fix_id="fx1", now=datetime(2026, 8, 1))

    assert await is_already_notified(conn, ticket_id="tk1", fix_id="fx1") is True


async def test_notification_is_scoped_to_the_specific_ticket_fix_pair():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ticket_fix_notifications_schema(conn)

    await mark_notified(conn, ticket_id="tk1", fix_id="fx1", now=datetime(2026, 8, 1))

    assert await is_already_notified(conn, ticket_id="tk1", fix_id="fx2") is False
    assert await is_already_notified(conn, ticket_id="tk2", fix_id="fx1") is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_ticket_fix_notifications.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.ticket_fix_notifications'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/ticket_fix_notifications.py`：

```python
from __future__ import annotations

from datetime import datetime

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticket_fix_notifications (
    ticket_id TEXT NOT NULL,
    fix_id TEXT NOT NULL,
    notified_at REAL NOT NULL,
    PRIMARY KEY (ticket_id, fix_id)
);
"""


async def ensure_ticket_fix_notifications_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def is_already_notified(conn: aiosqlite.Connection, *, ticket_id: str, fix_id: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM ticket_fix_notifications WHERE ticket_id = ? AND fix_id = ?",
        (ticket_id, fix_id),
    )
    row = await cursor.fetchone()
    return row is not None


async def mark_notified(
    conn: aiosqlite.Connection, *, ticket_id: str, fix_id: str, now: datetime
) -> None:
    await conn.execute(
        "INSERT INTO ticket_fix_notifications (ticket_id, fix_id, notified_at) "
        "VALUES (?, ?, ?) ON CONFLICT(ticket_id, fix_id) DO NOTHING",
        (ticket_id, fix_id, now.timestamp()),
    )
    await conn.commit()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_ticket_fix_notifications.py -v`
Expected: 3 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/ticket_fix_notifications.py tests/memory/test_ticket_fix_notifications.py
git commit -m "feat: add per-(ticket,fix) notification dedupe table"
```

---

### Task 8：新增工单查询 + 已知修复扫描编排

**Files:**
- Modify: `app/agent/create_ticket_tool.py`
- Modify: `app/memory/proactive_scan.py`
- Test: `tests/agent/test_create_ticket_tool.py`
- Test: `tests/memory/test_proactive_scan.py`

**Interfaces:**
- Consumes: `known_fixes.py`（Task 6）、`ticket_fix_notifications.py`（Task 7）、`send_followup_if_allowed`/`FollowupTrigger`（已有，`app/memory/followup_engine.py`）、`get_customer_profile`（已有，`app/memory/customer_profile.py`）
- Produces: `create_ticket_tool.py` 新增 `async def list_pending_tickets_created_before(conn, *, tenant_id: str, before: datetime) -> list[dict[str, Any]]`；`proactive_scan.py` 新增 `async def scan_and_send_known_fix_followups(conn, *, tenant_id: str, channel: ProactiveDeliveryChannel, embedding_registry: EmbeddingRegistry, embedding_provider_name: str, llm_registry: ProviderRegistry, llm_provider_name: str, now: datetime, similarity_threshold: float = 0.5) -> int`

- [ ] **Step 1: 写失败测试（工单查询部分）**

在 `tests/agent/test_create_ticket_tool.py` 末尾追加：

```python
async def test_list_pending_tickets_created_before_a_timestamp():
    conn = await aiosqlite.connect(":memory:")
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    old_ticket = await create_ticket(
        tenant_id="t1", customer_id="c1", question="旧问题",
        reason="原因", conn=conn, now=datetime(2026, 8, 1, 0, 0, 0),
    )
    await create_ticket(
        tenant_id="t1", customer_id="c2", question="新问题（修复之后才提的）",
        reason="原因", conn=conn, now=datetime(2026, 8, 6, 0, 0, 0),
    )

    results = await list_pending_tickets_created_before(conn, tenant_id="t1", before=fixed_at)

    assert len(results) == 1
    assert results[0]["ticket_id"] == old_ticket.ticket_id
```

（顶部需要新增 import：`from app.agent.create_ticket_tool import list_pending_tickets_created_before`，与已有的 `from app.agent.create_ticket_tool import (create_ticket, ensure_ticket_schema, list_stale_pending_tickets, mark_ticket_notified)` 合并成一个 import 语句即可）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_create_ticket_tool.py -v -k created_before`
Expected: `ImportError: cannot import name 'list_pending_tickets_created_before'`

- [ ] **Step 3: 写最小实现（工单查询部分）**

在 `app/agent/create_ticket_tool.py` 的 `list_stale_pending_tickets` 函数之后追加：

```python
async def list_pending_tickets_created_before(
    conn: aiosqlite.Connection, *, tenant_id: str, before: datetime
) -> list[dict[str, Any]]:
    """找出还是 pending 状态、创建时间早于给定时间点的工单——"已知故障
    修复后主动告知"的候选池：只有在修复上线之前提的工单才可能是同一个
    问题，修复之后才提的大概率是别的问题，不参与匹配。

    不排除 notified_at（"挂起过久"触发专用的标记），因为已知修复是完全
    不同的触发原因，去重靠调用方结合 ticket_fix_notifications 表按
    (ticket_id, fix_id) 维度判断，不能复用这个字段。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM tickets WHERE tenant_id = ? AND status = 'pending' "
        "AND created_at < ? ORDER BY created_at",
        (tenant_id, before.timestamp()),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_create_ticket_tool.py -v`
Expected: 全部通过

- [ ] **Step 5: 写失败测试（扫描编排部分）**

在 `tests/memory/test_proactive_scan.py` 末尾追加（复用文件已有的 `_llm_registry`/`ScriptedLLMProvider`/`MockProactiveChannel` 等 helper，无需重复定义）：

```python
from app.memory.known_fixes import ensure_known_fixes_schema, register_known_fix
from app.memory.ticket_fix_notifications import ensure_ticket_fix_notifications_schema, is_already_notified
from app.memory.proactive_scan import scan_and_send_known_fix_followups
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


class FixedEmbeddingProvider:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[self._vector for _ in request.texts])


def _embedding_registry(vector: list[float]) -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register("fake-embedding", FixedEmbeddingProvider(vector))
    return registry


async def test_scan_and_send_known_fix_followups_matches_similar_ticket():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    embedding_registry = _embedding_registry([1.0, 0.0])
    fix_id = await register_known_fix(
        conn, tenant_id="t1", description="网关超时问题已修复", fixed_at=fixed_at,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
    )
    await create_ticket(
        tenant_id="t1", customer_id="c1", question="网关超时报错E502",
        reason="原因", conn=conn, now=fixed_at - timedelta(days=1),
    )

    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["您反馈的网关超时问题已经修复，感谢您的耐心等待。"])

    sent = await scan_and_send_known_fix_followups(
        conn, tenant_id="t1", channel=channel,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 1
    assert len(channel.sent) == 1
    tickets = await list_pending_tickets_created_before(conn, tenant_id="t1", before=now)
    assert await is_already_notified(conn, ticket_id=tickets[0]["ticket_id"], fix_id=fix_id) is True


async def test_scan_and_send_known_fix_followups_skips_dissimilar_ticket():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    fix_embedding_registry = _embedding_registry([1.0, 0.0])
    await register_known_fix(
        conn, tenant_id="t1", description="网关超时问题已修复", fixed_at=fixed_at,
        embedding_registry=fix_embedding_registry, embedding_provider_name="fake-embedding",
    )
    await create_ticket(
        tenant_id="t1", customer_id="c1", question="完全不相关的问题",
        reason="原因", conn=conn, now=fixed_at - timedelta(days=1),
    )

    channel = MockProactiveChannel()
    # 扫描时给一个和 fix embedding 正交的向量，模拟"语义不相关"
    scan_embedding_registry = _embedding_registry([0.0, 1.0])
    llm_registry = _llm_registry(["不应该被用到"])

    sent = await scan_and_send_known_fix_followups(
        conn, tenant_id="t1", channel=channel,
        embedding_registry=scan_embedding_registry, embedding_provider_name="fake-embedding",
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 0
    assert channel.sent == []


async def test_scan_and_send_known_fix_followups_excludes_tickets_created_after_fix():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    embedding_registry = _embedding_registry([1.0, 0.0])
    await register_known_fix(
        conn, tenant_id="t1", description="网关超时问题已修复", fixed_at=fixed_at,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
    )
    await create_ticket(
        tenant_id="t1", customer_id="c1", question="网关超时报错E502（修复之后才提的）",
        reason="原因", conn=conn, now=fixed_at + timedelta(days=1),
    )

    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["不应该被用到"])

    sent = await scan_and_send_known_fix_followups(
        conn, tenant_id="t1", channel=channel,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 0
```

（顶部需要确认 `datetime`/`timedelta`/`aiosqlite`/`create_ticket`/`list_pending_tickets_created_before` 已 import——`create_ticket` 已有；`list_pending_tickets_created_before` 需要从 `app.agent.create_ticket_tool` 新增导入）

- [ ] **Step 6: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_proactive_scan.py -v -k known_fix`
Expected: `ImportError: cannot import name 'scan_and_send_known_fix_followups'`

- [ ] **Step 7: 写最小实现（扫描编排部分）**

在 `app/memory/proactive_scan.py` 末尾追加：

```python
import math

from app.agent.create_ticket_tool import list_pending_tickets_created_before
from app.memory.known_fixes import ensure_known_fixes_schema, list_known_fixes
from app.memory.ticket_fix_notifications import (
    ensure_ticket_fix_notifications_schema,
    is_already_notified,
    mark_notified,
)
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def scan_and_send_known_fix_followups(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    channel: ProactiveDeliveryChannel,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    now: datetime,
    similarity_threshold: float = 0.5,
) -> int:
    """已知故障修复后主动告知：对每条登记过的 known_fix，找出修复之前
    提交、且还没为这条 fix 通知过的 pending 工单，语义相似度够高就主动
    告知客户问题已修复。

    不复用 tickets.notified_at（那是"挂起过久"触发专用标记）——同一张
    工单可能先被挂起过久通知过、之后又该被已修复通知，两者不能共用同一
    个布尔标记互相掩盖，去重靠独立的 ticket_fix_notifications 表按
    (ticket_id, fix_id) 维度判断。
    """
    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    await ensure_customer_profile_schema(conn)
    await ensure_followup_log_schema(conn)

    sent_count = 0
    for fix in await list_known_fixes(conn, tenant_id=tenant_id):
        fixed_at = datetime.fromtimestamp(fix["fixed_at"])
        candidates = await list_pending_tickets_created_before(
            conn, tenant_id=tenant_id, before=fixed_at
        )
        for ticket in candidates:
            if await is_already_notified(conn, ticket_id=ticket["ticket_id"], fix_id=fix["fix_id"]):
                continue

            embed_result = await embedding_registry.run(
                EmbeddingRequest(texts=[ticket["question"]]),
                provider_name=embedding_provider_name,
            )
            similarity = _cosine_similarity(embed_result.vectors[0], fix["embedding"])
            if similarity < similarity_threshold:
                continue

            customer_id = ticket["customer_id"]
            profile = await get_customer_profile(conn, tenant_id=tenant_id, customer_id=customer_id)
            policy = compute_delivery_policy(profile)
            send_history = await get_send_history(
                conn, tenant_id=tenant_id, customer_id=customer_id,
                since=now - timedelta(seconds=policy.window_seconds),
            )
            trigger = FollowupTrigger(
                reason="known_fix_available",
                context=f"您反馈的「{ticket['question']}」问题已修复",
            )
            result = await send_followup_if_allowed(
                trigger, tenant_id=tenant_id, customer_id=customer_id, profile=profile,
                send_history=send_history, now=now, channel=channel,
                llm_registry=llm_registry, llm_provider_name=llm_provider_name,
            )
            if result.sent:
                await record_followup_sent(conn, tenant_id=tenant_id, customer_id=customer_id, sent_at=now)
                await mark_notified(conn, ticket_id=ticket["ticket_id"], fix_id=fix["fix_id"], now=now)
                sent_count += 1
    return sent_count
```

（顶部检查 `timedelta`/`datetime` 已从现有 `from datetime import datetime, timedelta` import；`ProactiveDeliveryChannel`/`ProviderRegistry`/`FollowupTrigger`/`send_followup_if_allowed`/`get_customer_profile`/`compute_delivery_policy`/`get_send_history`/`record_followup_sent`/`ensure_customer_profile_schema`/`ensure_followup_log_schema` 均已在文件顶部 import，无需重复添加）

- [ ] **Step 8: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_proactive_scan.py -v`
Expected: 全部通过

- [ ] **Step 9: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 10: 提交**

```bash
git add app/agent/create_ticket_tool.py app/memory/proactive_scan.py tests/agent/test_create_ticket_tool.py tests/memory/test_proactive_scan.py
git commit -m "feat: scan and notify customers when a known fix matches their ticket"
```

---

### Task 9：`known_fix_cli.py` 管理入口

**Files:**
- Create: `app/memory/known_fix_cli.py`
- Test: `tests/memory/test_known_fix_cli.py`

**Interfaces:**
- Consumes: `register_known_fix`/`list_known_fixes`/`ensure_known_fixes_schema`（Task 6）、`build_memory_conn_from_settings`（已有，`app/memory/factory.py`）、`build_embedding_registry_from_settings`/`DEFAULT_EMBEDDING_PROVIDER_NAME`（已有，`app/providers/factory.py`）
- Produces: `async def cmd_register(*, tenant_id: str, description: str, fixed_at: datetime, conn: aiosqlite.Connection, embedding_registry: EmbeddingRegistry, embedding_provider_name: str) -> str`（返回 `fix_id`）；`async def cmd_list(*, tenant_id: str, conn: aiosqlite.Connection) -> list[dict[str, Any]]`

参照 `app/graphrag/review_cli.py`/`tests/graphrag/test_review_cli.py` 的既定模式：`cmd_*` 函数接收显式注入的 `conn`/`embedding_registry` 参数（可测试，不在函数内部构造真实依赖），只有 `_main()` 才用 `Settings()` 构造真实依赖并调用 `cmd_*`；打印输出放在 `_main()` 里，`cmd_*` 只返回数据，不做 I/O。

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_known_fix_cli.py`：

```python
from datetime import datetime

import aiosqlite

from app.memory.known_fix_cli import cmd_list, cmd_register
from app.memory.known_fixes import ensure_known_fixes_schema
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


def _registry() -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register("fake-embedding", FakeEmbeddingProvider())
    return registry


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_known_fixes_schema(conn)
    return conn


async def test_cmd_register_persists_and_returns_fix_id():
    conn = await _connect()

    fix_id = await cmd_register(
        tenant_id="t1",
        description="网关超时问题已修复",
        fixed_at=datetime(2026, 8, 5, 0, 0, 0),
        conn=conn,
        embedding_registry=_registry(),
        embedding_provider_name="fake-embedding",
    )

    assert fix_id

    fixes = await cmd_list(tenant_id="t1", conn=conn)
    assert len(fixes) == 1
    assert fixes[0]["fix_id"] == fix_id


async def test_cmd_list_returns_empty_when_no_fixes_registered():
    conn = await _connect()

    fixes = await cmd_list(tenant_id="t1", conn=conn)

    assert fixes == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_known_fix_cli.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.known_fix_cli'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/known_fix_cli.py`：

```python
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Any

import aiosqlite

from app.config.settings import Settings
from app.memory.factory import build_memory_conn_from_settings
from app.memory.known_fixes import ensure_known_fixes_schema, list_known_fixes, register_known_fix
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import DEFAULT_EMBEDDING_PROVIDER_NAME, build_embedding_registry_from_settings


async def cmd_register(
    *,
    tenant_id: str,
    description: str,
    fixed_at: datetime,
    conn: aiosqlite.Connection,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
) -> str:
    return await register_known_fix(
        conn,
        tenant_id=tenant_id,
        description=description,
        fixed_at=fixed_at,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
    )


async def cmd_list(*, tenant_id: str, conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    return await list_known_fixes(conn, tenant_id=tenant_id)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="已知故障修复登记管理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser("register", help="登记一条已知故障修复")
    register_parser.add_argument("--tenant-id", required=True)
    register_parser.add_argument("--description", required=True, help="修复内容描述")
    register_parser.add_argument(
        "--fixed-at", required=True, help="修复时间，ISO格式，如 2026-08-05T00:00:00"
    )

    list_parser = subparsers.add_parser("list", help="列出已登记的修复记录")
    list_parser.add_argument("--tenant-id", required=True)

    return parser.parse_args()


async def _main() -> None:
    """CLI 入口。

    用法：
      python -m app.memory.known_fix_cli register --tenant-id t1 --description "网关超时问题已修复" --fixed-at 2026-08-05T00:00:00
      python -m app.memory.known_fix_cli list --tenant-id t1
    """
    args = _parse_args()
    settings = Settings()
    conn = await build_memory_conn_from_settings(settings)
    await ensure_known_fixes_schema(conn)

    if args.command == "register":
        embedding_registry = build_embedding_registry_from_settings(settings)
        fix_id = await cmd_register(
            tenant_id=args.tenant_id,
            description=args.description,
            fixed_at=datetime.fromisoformat(args.fixed_at),
            conn=conn,
            embedding_registry=embedding_registry,
            embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        )
        print(f"已登记修复记录 fix_id={fix_id}")
    elif args.command == "list":
        fixes = await cmd_list(tenant_id=args.tenant_id, conn=conn)
        if not fixes:
            print("没有已登记的修复记录。")
        for fix in fixes:
            fixed_at = datetime.fromtimestamp(fix["fixed_at"])
            print(f"[{fix['fix_id']}] {fix['description']} (修复时间: {fixed_at.isoformat()})")


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_known_fix_cli.py -v`
Expected: 2 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/known_fix_cli.py tests/memory/test_known_fix_cli.py
git commit -m "feat: add CLI for registering known fixes"
```

---

## 阶段 4：客户说"稍后再试"后到时确认

### Task 10：`delayed_confirmation` 表

**Files:**
- Create: `app/memory/delayed_confirmation.py`
- Test: `tests/memory/test_delayed_confirmation.py`

**Interfaces:**
- Produces: `ensure_delayed_confirmation_schema(conn)`；`async def schedule_delayed_confirmation(conn, *, tenant_id: str, user_id: str, context: str, confirm_after: datetime) -> str`（返回新记录 id）；`async def list_due_confirmations(conn, *, tenant_id: str, now: datetime) -> list[dict[str, Any]]`；`async def mark_confirmed(conn, *, confirmation_id: str, now: datetime) -> None`

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_delayed_confirmation.py`：

```python
from datetime import datetime, timedelta

import aiosqlite

from app.memory.delayed_confirmation import (
    ensure_delayed_confirmation_schema,
    list_due_confirmations,
    mark_confirmed,
    schedule_delayed_confirmation,
)


async def test_list_due_confirmations_returns_only_due_and_unconfirmed():
    conn = await aiosqlite.connect(":memory:")
    await ensure_delayed_confirmation_schema(conn)
    now = datetime(2026, 8, 6, 10, 0, 0)

    due_id = await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="u1", context="重启路由器试试",
        confirm_after=now - timedelta(hours=1),
    )
    await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="u2", context="还没到期的",
        confirm_after=now + timedelta(hours=1),
    )

    due = await list_due_confirmations(conn, tenant_id="t1", now=now)

    assert len(due) == 1
    assert due[0]["id"] == due_id
    assert due[0]["context"] == "重启路由器试试"
    assert due[0]["user_id"] == "u1"


async def test_mark_confirmed_excludes_it_from_due_list():
    conn = await aiosqlite.connect(":memory:")
    await ensure_delayed_confirmation_schema(conn)
    now = datetime(2026, 8, 6, 10, 0, 0)

    confirmation_id = await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="u1", context="重启路由器试试",
        confirm_after=now - timedelta(hours=1),
    )
    await mark_confirmed(conn, confirmation_id=confirmation_id, now=now)

    due = await list_due_confirmations(conn, tenant_id="t1", now=now)
    assert due == []


async def test_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    await ensure_delayed_confirmation_schema(conn)
    now = datetime(2026, 8, 6, 10, 0, 0)

    await schedule_delayed_confirmation(
        conn, tenant_id="t2", user_id="u1", context="别的租户",
        confirm_after=now - timedelta(hours=1),
    )

    due = await list_due_confirmations(conn, tenant_id="t1", now=now)
    assert due == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_delayed_confirmation.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.delayed_confirmation'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/delayed_confirmation.py`：

```python
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS delayed_confirmations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    context TEXT NOT NULL,
    confirm_after REAL NOT NULL,
    confirmed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_delayed_confirmations_due
    ON delayed_confirmations (tenant_id, confirmed_at, confirm_after);
"""


async def ensure_delayed_confirmation_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def schedule_delayed_confirmation(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    context: str,
    confirm_after: datetime,
) -> str:
    confirmation_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO delayed_confirmations (id, tenant_id, user_id, context, confirm_after) "
        "VALUES (?, ?, ?, ?, ?)",
        (confirmation_id, tenant_id, user_id, context, confirm_after.timestamp()),
    )
    await conn.commit()
    return confirmation_id


async def list_due_confirmations(
    conn: aiosqlite.Connection, *, tenant_id: str, now: datetime
) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM delayed_confirmations WHERE tenant_id = ? "
        "AND confirmed_at IS NULL AND confirm_after <= ? ORDER BY confirm_after",
        (tenant_id, now.timestamp()),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_confirmed(conn: aiosqlite.Connection, *, confirmation_id: str, now: datetime) -> None:
    await conn.execute(
        "UPDATE delayed_confirmations SET confirmed_at = ? WHERE id = ?",
        (now.timestamp(), confirmation_id),
    )
    await conn.commit()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_delayed_confirmation.py -v`
Expected: 3 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/delayed_confirmation.py tests/memory/test_delayed_confirmation.py
git commit -m "feat: add delayed_confirmations schedule for follow-up on delay intent"
```

---

### Task 11：延迟意图检测

**Files:**
- Create: `app/memory/delay_intent.py`
- Test: `tests/memory/test_delay_intent.py`

**Interfaces:**
- Produces: `async def detect_delay_intent(text: str, *, llm_registry: ProviderRegistry, llm_provider_name: str, timeout_sec: float = 2.0) -> bool`

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_delay_intent.py`（结构与 `test_correction_intent.py` 一致）：

```python
from app.memory.delay_intent import detect_delay_intent
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider 挂了")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_detects_delay_intent_when_llm_says_true():
    result = await detect_delay_intent(
        "我先按您说的重启路由器试试，不行再联系",
        llm_registry=_registry(FixedLLMProvider('{"is_delay": true}')),
        llm_provider_name="llm",
    )
    assert result is True


async def test_does_not_detect_delay_intent_for_normal_question():
    result = await detect_delay_intent(
        "网络连不上怎么办",
        llm_registry=_registry(FixedLLMProvider('{"is_delay": false}')),
        llm_provider_name="llm",
    )
    assert result is False


async def test_falls_back_to_rule_when_llm_fails():
    result = await detect_delay_intent(
        "我先试试，稍后再试",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
    )
    assert result is True


async def test_rule_fallback_does_not_flag_normal_question():
    result = await detect_delay_intent(
        "网络连不上怎么办",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
    )
    assert result is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_delay_intent.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.delay_intent'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/delay_intent.py`：

```python
from __future__ import annotations

import asyncio
import json
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是客服对话意图分类器。判断用户这句话是否表达了“稍后再自己尝试、"
    "之后需要跟进确认结果”的意图（例如“我先试试”“稍后再试”“待会弄”）。"
    '只输出 JSON：{"is_delay": true/false}。'
)

_DELAY_KEYWORDS = ("稍后再试", "待会试试", "过会儿再弄", "我先试试", "先试试")


def _looks_like_delay_by_rule(text: str) -> bool:
    return any(keyword in text for keyword in _DELAY_KEYWORDS)


async def detect_delay_intent(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> bool:
    """判断这句话是不是"稍后自己先试试、之后需要跟进确认"的意图；LLM
    失败/超时/解析失败时降级为关键词规则兜底。
    """
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("延迟意图检测超时，回退规则判断")
        return _looks_like_delay_by_rule(text)
    except Exception:
        logger.warning("延迟意图检测失败，回退规则判断", exc_info=True)
        return _looks_like_delay_by_rule(text)

    try:
        payload = json.loads(result.text)
        return bool(payload.get("is_delay", False))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return _looks_like_delay_by_rule(text)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_delay_intent.py -v`
Expected: 4 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/delay_intent.py tests/memory/test_delay_intent.py
git commit -m "feat: add delay-intent detection for follow-up scheduling"
```

---

### Task 12：接入 `run_memory_consolidation`

**Files:**
- Modify: `app/memory/consolidation.py`
- Test: `tests/memory/test_consolidation.py`

**Interfaces:**
- Consumes: `detect_delay_intent(...)`（Task 11）、`schedule_delayed_confirmation(...)`（Task 10）、`resolve_time_window(text, *, llm_registry, llm_provider_name, reference_time, min_confidence=0.5, timeout_sec=2.0) -> TimeWindowResult`（已有）
- Produces: `run_memory_consolidation()` 新增可选参数 `now: datetime | None = None`；副作用：检测到延迟意图时写入一条 `delayed_confirmations` 记录（不影响返回值 `list[dict[str,str]]`，也不受 `facts` 是否为空影响）

- [ ] **Step 1: 写失败测试**

打开现有 `tests/memory/test_consolidation.py`（若不存在，创建；先用 `Glob` 确认路径是否存在于 `tests/memory/` 下，若已有同名文件则在文件末尾追加，不要覆盖已有内容），追加：

```python
from datetime import datetime, timedelta

from app.memory.delayed_confirmation import ensure_delayed_confirmation_schema, list_due_confirmations


async def test_run_memory_consolidation_schedules_delayed_confirmation_on_delay_intent(tmp_conn=None):
    import aiosqlite

    from app.memory.schema import ensure_schema

    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await ensure_delayed_confirmation_schema(conn)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": true}',  # detect_delay_intent
                '{"start": null, "end": null, "confidence": 0}',  # resolve_time_window（低置信度，规则引擎也无法解析"先试试"这种非时间表达，回退默认2小时）
                '{"facts": []}',  # fact_extractor（这句话本身没有值得记忆的事实）
            ]
        ),
    )
    now = datetime(2026, 8, 6, 10, 0, 0)

    await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="我先按您说的重启路由器试试，不行再联系",
        assistant_output="好的，麻烦您先试试，有问题随时联系我们。",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
    )

    due = await list_due_confirmations(conn, tenant_id="t1", now=now + timedelta(hours=3))
    assert len(due) == 1
    assert due[0]["user_id"] == "u1"
    assert "重启路由器试试" in due[0]["context"]


async def test_run_memory_consolidation_does_not_schedule_for_normal_turn():
    import aiosqlite

    from app.memory.schema import ensure_schema

    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await ensure_delayed_confirmation_schema(conn)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": false}',  # detect_delay_intent
                '{"facts": []}',  # fact_extractor
            ]
        ),
    )
    now = datetime(2026, 8, 6, 10, 0, 0)

    await run_memory_consolidation(
        conn,
        tenant_id="t1",
        user_id="u1",
        user_input="网络连不上怎么办",
        assistant_output="请先重启路由器。",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
    )

    due = await list_due_confirmations(conn, tenant_id="t1", now=now + timedelta(hours=3))
    assert due == []
```

如果 `tests/memory/test_consolidation.py` 尚不存在或顶部缺少 `run_memory_consolidation`/`ProviderRegistry`/`ProviderCapability`/`ScriptedLLMProvider` 的 import/定义，在文件顶部补充：

```python
from app.memory.consolidation import run_memory_consolidation
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))
```

（若文件已存在且已有同名 `ScriptedLLMProvider`/相关 import，直接复用，不要重复定义）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_consolidation.py -v -k delayed_confirmation`
Expected: `TypeError: run_memory_consolidation() got an unexpected keyword argument 'now'`

- [ ] **Step 3: 写最小实现**

修改 `app/memory/consolidation.py`：

顶部新增 import：

```python
from datetime import datetime, timedelta

from app.memory.delay_intent import detect_delay_intent
from app.memory.delayed_confirmation import schedule_delayed_confirmation
from app.memory.temporal_resolver import resolve_time_window
```

`run_memory_consolidation` 签名新增 `now: datetime | None = None` 参数，并在函数体最前面（`facts = await extract_facts(...)` 之前）插入延迟意图检测逻辑：

```python
async def run_memory_consolidation(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    user_id: str,
    user_input: str,
    assistant_output: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    fact_extract_timeout_sec: float = 2.0,
    conflict_resolve_timeout_sec: float = 2.0,
    embedding_registry: EmbeddingRegistry | None = None,
    embedding_provider_name: str | None = None,
    similarity_top_k: int = 20,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """..."""  # 保留原有 docstring 不变，可在末尾追加一段：
    # """
    # now 为可选项：用于延迟意图检测（"稍后再试"类话语）解析出的确认时间
    # 计算基准，不提供则用 datetime.now()——这一步和事实抽取/冲突决策相互
    # 独立，即使这轮对话抽不出任何长期记忆事实也照常执行，不受 `facts`
    # 是否为空影响。
    # """
    resolved_now = now or datetime.now()
    is_delay = await detect_delay_intent(
        user_input, llm_registry=llm_registry, llm_provider_name=llm_provider_name
    )
    if is_delay:
        time_result = await resolve_time_window(
            user_input,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            reference_time=resolved_now,
        )
        if time_result.resolved and time_result.start and time_result.start > resolved_now:
            confirm_after = time_result.start
        else:
            confirm_after = resolved_now + timedelta(hours=2)
        await schedule_delayed_confirmation(
            conn, tenant_id=tenant_id, user_id=user_id,
            context=user_input, confirm_after=confirm_after,
        )

    facts = await extract_facts(
        user_input=user_input,
        assistant_output=assistant_output,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        timeout_sec=fact_extract_timeout_sec,
    )
    if not facts:
        return []
    # ... 其余逻辑不变
```

（保留函数体剩余部分——`if embedding_registry is not None ...` 到函数结尾——完全不变，只在函数最前面插入上面这段新逻辑）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_consolidation.py -v`
Expected: 全部通过

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过（若既有的 `test_consolidation.py`/`test_consolidation_queue.py`/`test_graph_memory.py` 里对 `run_memory_consolidation` 的 scripted LLM 调用次数断言因为新增了 `detect_delay_intent` 调用而失衡，按失败信息补一条 `'{"is_delay": false}'` 的 scripted response 到对应测试的响应列表最前面）

- [ ] **Step 6: 提交**

```bash
git add app/memory/consolidation.py tests/memory/test_consolidation.py
git commit -m "feat: schedule delayed confirmation from delay-intent detection in consolidation"
```

---

### Task 13：到期确认扫描编排

**Files:**
- Modify: `app/memory/proactive_scan.py`
- Test: `tests/memory/test_proactive_scan.py`

**Interfaces:**
- Consumes: `list_due_confirmations`/`mark_confirmed`（Task 10）、`send_followup_if_allowed`/`FollowupTrigger`（已有）
- Produces: `async def scan_and_send_delayed_confirmation_followups(conn, *, tenant_id: str, channel: ProactiveDeliveryChannel, llm_registry: ProviderRegistry, llm_provider_name: str, now: datetime) -> int`

- [ ] **Step 1: 写失败测试**

在 `tests/memory/test_proactive_scan.py` 末尾追加：

```python
from app.memory.delayed_confirmation import ensure_delayed_confirmation_schema, schedule_delayed_confirmation
from app.memory.proactive_scan import scan_and_send_delayed_confirmation_followups


async def test_scan_and_send_delayed_confirmation_followups_sends_when_due():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    await ensure_delayed_confirmation_schema(conn)
    await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="c1", context="重启路由器试试",
        confirm_after=now - timedelta(hours=1),
    )

    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["您好，想确认一下之前的问题现在解决了吗？"])

    sent = await scan_and_send_delayed_confirmation_followups(
        conn, tenant_id="t1", channel=channel,
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 1
    assert len(channel.sent) == 1


async def test_scan_and_send_delayed_confirmation_followups_skips_not_due_yet():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    await ensure_delayed_confirmation_schema(conn)
    await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="c1", context="还没到期",
        confirm_after=now + timedelta(hours=1),
    )

    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["不应该被用到"])

    sent = await scan_and_send_delayed_confirmation_followups(
        conn, tenant_id="t1", channel=channel,
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 0
    assert channel.sent == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_proactive_scan.py -v -k delayed_confirmation_followups`
Expected: `ImportError: cannot import name 'scan_and_send_delayed_confirmation_followups'`

- [ ] **Step 3: 写最小实现**

在 `app/memory/proactive_scan.py` 末尾追加：

```python
from app.memory.delayed_confirmation import (
    ensure_delayed_confirmation_schema,
    list_due_confirmations,
    mark_confirmed,
)


async def scan_and_send_delayed_confirmation_followups(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    channel: ProactiveDeliveryChannel,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    now: datetime,
) -> int:
    """客户说"稍后再试"到期后主动确认结果。"""
    await ensure_delayed_confirmation_schema(conn)
    await ensure_customer_profile_schema(conn)
    await ensure_followup_log_schema(conn)

    sent_count = 0
    for item in await list_due_confirmations(conn, tenant_id=tenant_id, now=now):
        customer_id = item["user_id"]
        profile = await get_customer_profile(conn, tenant_id=tenant_id, customer_id=customer_id)
        policy = compute_delivery_policy(profile)
        send_history = await get_send_history(
            conn, tenant_id=tenant_id, customer_id=customer_id,
            since=now - timedelta(seconds=policy.window_seconds),
        )
        trigger = FollowupTrigger(
            reason="delayed_confirmation",
            context=f"之前您提到{item['context']}，想确认一下现在情况如何？",
        )
        result = await send_followup_if_allowed(
            trigger, tenant_id=tenant_id, customer_id=customer_id, profile=profile,
            send_history=send_history, now=now, channel=channel,
            llm_registry=llm_registry, llm_provider_name=llm_provider_name,
        )
        if result.sent:
            await record_followup_sent(conn, tenant_id=tenant_id, customer_id=customer_id, sent_at=now)
            await mark_confirmed(conn, confirmation_id=item["id"], now=now)
            sent_count += 1
    return sent_count
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_proactive_scan.py -v`
Expected: 全部通过

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/proactive_scan.py tests/memory/test_proactive_scan.py
git commit -m "feat: scan and confirm delayed follow-ups after their due time"
```

---

## 阶段 5：Redis 可插拔会话滑窗后端

### Task 14：`SessionWindowStore` 协议 + SQLite 默认实现

**Files:**
- Create: `app/memory/session_window_store.py`
- Test: `tests/memory/test_session_window_store.py`

**Interfaces:**
- Consumes: `append_turn`/`get_recent_turns`（已有，`app/memory/session_window.py`）
- Produces: `class SessionWindowStore(Protocol)`；`class SQLiteSessionWindowStore` 实现该协议

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_session_window_store.py`：

```python
import aiosqlite

from app.memory.schema import ensure_schema
from app.memory.session_window_store import SQLiteSessionWindowStore


async def test_sqlite_store_appends_and_reads_back_turns():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    store = SQLiteSessionWindowStore(conn)

    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="user", content="你好")
    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="assistant", content="您好，有什么可以帮您")

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "你好"


async def test_sqlite_store_scoped_to_session():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    store = SQLiteSessionWindowStore(conn)

    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="user", content="会话1")
    await store.append_turn(tenant_id="t1", session_id="s2", user_id="u1", role="user", content="会话2")

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert len(turns) == 1
    assert turns[0]["content"] == "会话1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_session_window_store.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.session_window_store'`

- [ ] **Step 3: 写最小实现**

创建 `app/memory/session_window_store.py`：

```python
from __future__ import annotations

from typing import Any, Protocol

import aiosqlite

from app.memory.session_window import append_turn, get_recent_turns


class SessionWindowStore(Protocol):
    async def append_turn(
        self, *, tenant_id: str, session_id: str, user_id: str, role: str, content: str
    ) -> None: ...

    async def get_recent_turns(
        self, *, tenant_id: str, session_id: str, limit: int
    ) -> list[dict[str, Any]]: ...


class SQLiteSessionWindowStore:
    """薄封装现有 app/memory/session_window.py 自由函数，零行为变化——
    默认实现，不配置 Redis 时走这条路径。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def append_turn(
        self, *, tenant_id: str, session_id: str, user_id: str, role: str, content: str
    ) -> None:
        await append_turn(
            self._conn, tenant_id=tenant_id, session_id=session_id,
            user_id=user_id, role=role, content=content,
        )

    async def get_recent_turns(
        self, *, tenant_id: str, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        return await get_recent_turns(
            self._conn, tenant_id=tenant_id, session_id=session_id, limit=limit
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_session_window_store.py -v`
Expected: 2 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/session_window_store.py tests/memory/test_session_window_store.py
git commit -m "feat: add SessionWindowStore protocol with SQLite default implementation"
```

---

### Task 15：`RedisSessionWindowStore`

**Files:**
- Modify: `app/memory/session_window_store.py`
- Test: `tests/memory/test_session_window_store.py`

**Interfaces:**
- Produces: `class RedisClientProtocol(Protocol)`（`rpush`/`ltrim`/`lrange`/`expire`）；`class RedisSessionWindowStore` 实现 `SessionWindowStore`

- [ ] **Step 1: 写失败测试**

在 `tests/memory/test_session_window_store.py` 末尾追加：

```python
from app.memory.session_window_store import RedisSessionWindowStore


class FakeRedisClient:
    """纯 Python 字典实现的假 Redis 客户端，只实现本次用到的 4 个命令，
    不需要真实 Redis 服务。"""

    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def rpush(self, key: str, value: str) -> None:
        self._lists.setdefault(key, []).append(value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        values = self._lists.get(key, [])
        # Redis LTRIM 语义：end=-1 表示到末尾，start 为负数表示从末尾数
        length = len(values)
        normalized_start = start if start >= 0 else max(length + start, 0)
        normalized_end = length - 1 if end == -1 else (end if end >= 0 else length + end)
        self._lists[key] = values[normalized_start : normalized_end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self._lists.get(key, [])
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    async def expire(self, key: str, ttl_seconds: int) -> None:
        self.expire_calls.append((key, ttl_seconds))


async def test_redis_store_appends_and_reads_back_turns():
    client = FakeRedisClient()
    store = RedisSessionWindowStore(client, max_turns=50, ttl_seconds=86400)

    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="user", content="你好")
    await store.append_turn(tenant_id="t1", session_id="s1", user_id="u1", role="assistant", content="您好")

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "你好"
    assert client.expire_calls[-1] == ("session_turns:t1:s1", 86400)


async def test_redis_store_trims_to_max_turns():
    client = FakeRedisClient()
    store = RedisSessionWindowStore(client, max_turns=2, ttl_seconds=86400)

    for i in range(5):
        await store.append_turn(
            tenant_id="t1", session_id="s1", user_id="u1", role="user", content=f"消息{i}"
        )

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=10)

    assert len(turns) == 2
    assert [t["content"] for t in turns] == ["消息3", "消息4"]


async def test_redis_store_respects_get_recent_turns_limit():
    client = FakeRedisClient()
    store = RedisSessionWindowStore(client, max_turns=50, ttl_seconds=86400)

    for i in range(5):
        await store.append_turn(
            tenant_id="t1", session_id="s1", user_id="u1", role="user", content=f"消息{i}"
        )

    turns = await store.get_recent_turns(tenant_id="t1", session_id="s1", limit=2)

    assert [t["content"] for t in turns] == ["消息3", "消息4"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_session_window_store.py -v -k redis`
Expected: `ImportError: cannot import name 'RedisSessionWindowStore'`

- [ ] **Step 3: 写最小实现**

在 `app/memory/session_window_store.py` 末尾追加：

```python
import json


class RedisClientProtocol(Protocol):
    async def rpush(self, key: str, value: str) -> None: ...
    async def ltrim(self, key: str, start: int, end: int) -> None: ...
    async def lrange(self, key: str, start: int, end: int) -> list[str]: ...
    async def expire(self, key: str, ttl_seconds: int) -> None: ...


class RedisSessionWindowStore:
    """会话滑窗 Redis 实现：key = f"session_turns:{tenant_id}:{session_id}"，
    每条轮次序列化为 JSON 存进一个 Redis List，RPUSH 追加 + LTRIM 只保留
    最近 max_turns 条 + EXPIRE 每次写入都刷新滑动过期时间。
    """

    def __init__(
        self, redis_client: RedisClientProtocol, *, max_turns: int = 50, ttl_seconds: int = 86400
    ) -> None:
        self._client = redis_client
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds

    def _key(self, *, tenant_id: str, session_id: str) -> str:
        return f"session_turns:{tenant_id}:{session_id}"

    async def append_turn(
        self, *, tenant_id: str, session_id: str, user_id: str, role: str, content: str
    ) -> None:
        key = self._key(tenant_id=tenant_id, session_id=session_id)
        payload = json.dumps({"role": role, "content": content})
        await self._client.rpush(key, payload)
        await self._client.ltrim(key, -self._max_turns, -1)
        await self._client.expire(key, self._ttl_seconds)

    async def get_recent_turns(
        self, *, tenant_id: str, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        key = self._key(tenant_id=tenant_id, session_id=session_id)
        raw_values = await self._client.lrange(key, -limit, -1)
        return [json.loads(value) for value in raw_values]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_session_window_store.py -v`
Expected: 5 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/memory/session_window_store.py tests/memory/test_session_window_store.py
git commit -m "feat: add Redis-backed session window store implementation"
```

---

### Task 16：Settings + 工厂函数

**Files:**
- Modify: `app/config/settings.py`
- Create: `app/memory/session_window_factory.py`
- Test: `tests/memory/test_session_window_factory.py`

**Interfaces:**
- Consumes: `SQLiteSessionWindowStore`/`RedisSessionWindowStore`（Task 14/15）
- Produces: `Settings.session_window_backend: str = "sqlite"`；`Settings.redis_url: str | None = None`；`def build_session_window_store_from_settings(settings: Settings, *, memory_conn: aiosqlite.Connection) -> SessionWindowStore`

- [ ] **Step 1: 写失败测试**

创建 `tests/memory/test_session_window_factory.py`：

```python
import aiosqlite

from app.config.settings import Settings
from app.memory.session_window_factory import build_session_window_store_from_settings
from app.memory.session_window_store import RedisSessionWindowStore, SQLiteSessionWindowStore


def _base_kwargs() -> dict:
    return dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
    )


async def test_defaults_to_sqlite_backend():
    conn = await aiosqlite.connect(":memory:")
    settings = Settings(**_base_kwargs())

    store = build_session_window_store_from_settings(settings, memory_conn=conn)

    assert isinstance(store, SQLiteSessionWindowStore)


async def test_uses_redis_backend_when_configured():
    conn = await aiosqlite.connect(":memory:")
    settings = Settings(
        **_base_kwargs(), session_window_backend="redis", redis_url="redis://localhost:6379/0"
    )

    store = build_session_window_store_from_settings(settings, memory_conn=conn)

    assert isinstance(store, RedisSessionWindowStore)


async def test_raises_immediately_when_redis_backend_missing_url():
    conn = await aiosqlite.connect(":memory:")
    settings = Settings(**_base_kwargs(), session_window_backend="redis", redis_url=None)

    try:
        build_session_window_store_from_settings(settings, memory_conn=conn)
        assert False, "应该在构建时就报错，而不是等到运行时"
    except ValueError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_session_window_factory.py -v`
Expected: `ModuleNotFoundError: No module named 'app.memory.session_window_factory'`

- [ ] **Step 3: 写最小实现**

修改 `app/config/settings.py`，在 `agent_min_relevance_score: float | None = None` 之后追加：

```python
    # 会话滑窗存储后端："sqlite"（默认，复用 memory_conn）或 "redis"
    # （并发扩展性考虑，见 app/memory/session_window_store.py）。
    session_window_backend: str = "sqlite"
    redis_url: str | None = None
```

创建 `app/memory/session_window_factory.py`：

```python
from __future__ import annotations

import aiosqlite

from app.config.settings import Settings
from app.memory.session_window_store import (
    RedisSessionWindowStore,
    SessionWindowStore,
    SQLiteSessionWindowStore,
)


def build_session_window_store_from_settings(
    settings: Settings, *, memory_conn: aiosqlite.Connection
) -> SessionWindowStore:
    """session_window_backend="redis" 时需要 redis_url，缺失就立即报错
    （构建时失败，不拖到运行时某次 append_turn 才暴露配置问题）；
    默认（或任何非 "redis" 的值）走 SQLiteSessionWindowStore，复用同一个
    memory_conn，不引入额外连接。
    """
    if settings.session_window_backend == "redis":
        if not settings.redis_url:
            raise ValueError(
                "session_window_backend='redis' 时必须配置 redis_url"
            )
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url)
        return RedisSessionWindowStore(client)
    return SQLiteSessionWindowStore(memory_conn)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/memory/test_session_window_factory.py -v`
Expected: 3 passed（若 `test_uses_redis_backend_when_configured` 因为环境没装 `redis` 包报 `ModuleNotFoundError`，先执行 `.venv/Scripts/python.exe -m pip install "redis>=5.0"` 再重跑）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/config/settings.py app/memory/session_window_factory.py tests/memory/test_session_window_factory.py pyproject.toml
git commit -m "feat: add pluggable session-window backend selection via settings"
```

（记得在 `pyproject.toml` 的 `dependencies` 列表里补一行 `"redis>=5.0",`，紧跟在 `"dashscope>=1.20",` 之后，作为可选但声明的依赖——即使默认不用 Redis，装了这个包也不会影响 SQLite 路径的任何行为）

---

### Task 17：接入 `graph.py`

**Files:**
- Modify: `app/agent/graph.py`
- Test: `tests/agent/test_graph_memory.py`

**Interfaces:**
- Consumes: `SessionWindowStore`（Task 14/15）、`build_session_window_store_from_settings`（Task 16）
- Produces: `build_agent_graph()` 新增可选参数 `session_window_store: SessionWindowStore | None = None`

不修改 `app/api/agent_routes.py`——默认不传 `session_window_store` 等价于现状（`SQLiteSessionWindowStore` 包装同一个 `memory_conn`），是否在路由层接入 Redis 留给实际部署方按需决定，见本任务末尾说明。

- [ ] **Step 1: 写失败测试**

在 `tests/agent/test_graph_memory.py` 末尾追加：

```python
async def test_uses_injected_session_window_store_instead_of_direct_sql():
    from app.memory.session_window_store import SessionWindowStore

    class RecordingSessionWindowStore:
        def __init__(self) -> None:
            self.appended: list[dict] = []

        async def append_turn(self, *, tenant_id, session_id, user_id, role, content):
            self.appended.append(
                {"tenant_id": tenant_id, "session_id": session_id, "role": role, "content": content}
            )

        async def get_recent_turns(self, *, tenant_id, session_id, limit):
            return [
                {"role": item["role"], "content": item["content"]}
                for item in self.appended
                if item["tenant_id"] == tenant_id and item["session_id"] == session_id
            ][-limit:]

    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    session_window_store = RecordingSessionWindowStore()

    embedding_registry, vector_store, bm25_index, llm_registry, llm_provider = (
        await _build_dependencies(["重启路由器即可解决。", '{"facts":[]}'])
    )
    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        memory_conn=conn,
        session_window_store=session_window_store,
    )

    await graph.ainvoke(
        {
            "question": "网络连不上怎么办？",
            "tenant_id": "t1",
            "session_id": "s1",
            "user_id": "u1",
        }
    )

    assert len(session_window_store.appended) == 2
    assert session_window_store.appended[0]["role"] == "user"
    assert session_window_store.appended[1]["role"] == "assistant"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_memory.py -v -k injected_session_window`
Expected: `TypeError: build_agent_graph() got an unexpected keyword argument 'session_window_store'`

- [ ] **Step 3: 写最小实现**

修改 `app/agent/graph.py`：

1. 顶部新增 import：

```python
from app.memory.session_window_store import SessionWindowStore, SQLiteSessionWindowStore
```

2. `build_agent_graph` 签名新增参数（放在 `on_answer_chunk` 之后）：

```python
    on_answer_chunk: Callable[[str], Awaitable[None]] | None = None,
    session_window_store: SessionWindowStore | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
```

3. 在函数体最前面（第一个 `async def input_safety_node` 之前）解析出实际使用的 store：

```python
    resolved_session_window_store: SessionWindowStore | None = None
    if memory_conn is not None:
        resolved_session_window_store = session_window_store or SQLiteSessionWindowStore(memory_conn)
```

4. 修改 `memory_save_node`，把两处 `await append_turn(memory_conn, ...)` 替换为：

```python
    async def memory_save_node(state: AgentState) -> dict[str, Any]:
        if memory_conn is None:
            return {}
        session_id = state.get("session_id", "")
        user_id = state.get("user_id", "")
        final_text = state.get("final_text", "")
        await resolved_session_window_store.append_turn(
            tenant_id=state["tenant_id"],
            session_id=session_id,
            user_id=user_id,
            role="user",
            content=state["question"],
        )
        await resolved_session_window_store.append_turn(
            tenant_id=state["tenant_id"],
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=final_text,
        )
        await enqueue_consolidation_job(
            memory_conn,
            tenant_id=state["tenant_id"],
            user_id=user_id,
            session_id=session_id,
            user_input=state["question"],
            assistant_output=final_text,
        )
        return {}
```

5. `context_injection.py` 里的 `get_recent_turns(conn, ...)` 调用暂不改动（`inject_memory_context` 独立接收 `conn` 参数，属于 `memory_recall_node` 调用链，这次只切换写入路径；读取路径的切换留到确认写入路径工作正常之后再做，避免一次改两个方向增加排查复杂度——如果需要同步切换读取路径，在这一步之后加一个 Task，把 `inject_memory_context` 也改成接收 `session_window_store` 参数）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_graph_memory.py -v`
Expected: 全部通过

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/agent/graph.py tests/agent/test_graph_memory.py
git commit -m "feat: wire pluggable session window store into memory_save_node"
```

**注**：`app/api/agent_routes.py` 的接线（`build_session_window_store_from_settings(settings, memory_conn=memory_conn)` 传给 `build_agent_graph(session_window_store=...)`）留给实际部署方在启用 Redis 时按需接入——默认不传等价于 `session_window_store=None`，行为和现在完全一致，本任务不强制修改 `agent_routes.py`。

---

## 跨阶段依赖说明

- 阶段 1（即时纠错通道）、阶段 2（P1结构化检索）互相独立，任意顺序执行均可
- 阶段 3（已知修复）Task 6-9 之间是严格顺序依赖（表结构 → 去重表 → 编排 → CLI）
- 阶段 4（稍后确认）Task 10-13 之间是严格顺序依赖（表结构 → 意图检测 → 接入consolidation → 编排）
- 阶段 3 和阶段 4 彼此独立，可以调换顺序或并行安排给不同开发者
- 阶段 5（Redis）Task 14-17 严格顺序依赖，且不依赖阶段 1-4 的任何产出，可以随时插入执行，也可以整体跳过（默认 SQLite 路径不受影响）
