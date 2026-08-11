# 问答链路 + 摄取吞吐 并发优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地《端到端流程时间最优性分析》报告里 P0/P1 优先级的并发化改造——修复 BM25 阻塞事件循环、把问答链路里彼此无数据依赖的调用改成并发、给跨文档摄取吞吐补上共享并发预算的基础设施。

**Architecture:** 沿用本仓库本会话已经验证过的模式：`asyncio.gather(..., return_exceptions=True)` + 事后手动 `raise` 保留原有失败语义（不用默认 gather 行为，避免一边失败时另一边裸跑）；每个并发化任务配一个用 `asyncio.Event`（或跨线程场景下用真实耗时差）证明"确实并发、不是退化回顺序执行"的测试，而不是只断言最终结果正确。

**Tech Stack:** Python 3.12 / asyncio / pytest-asyncio，不引入任何新依赖。

## Global Constraints

- 不改变任何函数在"正常路径"下的输出结果，只改变执行顺序/并发度——所有改造必须让现有测试套件（`python -m pytest tests/ -q`）保持全绿（除了会话开始前就存在、与本计划无关的 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`）。
- 失败语义必须逐字保留：改造前"某一步失败会让整个调用失败"的地方，改造后依然失败；改造前"某一步失败被内部吞掉"的地方，改造后依然被吞掉。
- 每个引入并发的任务都要有一个能在"退化回顺序执行"时失败的回归测试（deadlock 式 `asyncio.Event` 互等，或跨线程场景下的真实耗时差断言），不能只测最终返回值正确。
- 新增的可配置并发度参数一律保守默认（不改变现状），比照本仓库已有的 `table_extraction_max_concurrency` 从 1 起步、有真实压测数据支撑再抬高的先例。
- 中文注释只写"为什么"，不写"是什么"；不加多余的错误处理/校验；每个任务结束跑一次相关测试文件，全部任务完成后跑一次全量测试套件。

## 本计划不包含（分析报告里的 P2 + 一个刻意排除的 P1 子项）

以下三项在分析报告里出现过，本计划故意不做，原因写清楚而不是漏掉：

- **`_prepare_pdf_sync` 页级并行**（报告 P2）：值不值得做取决于 MuPDF 渲染/表格检测在 C 层是不是释放 GIL——这个前提本身还没验证过，在验证之前写不出真实的实现任务（"释放 GIL 就多线程、不释放就要多进程"是两条完全不同的实现路径）。需要先跑一次独立的受控实验（同一份文档，单线程 vs 拆两个线程各处理一半页面，对比总耗时是否接近减半），有结论之后再补一份单独的计划。
- **缓存层**（报告 P2）：收益取决于生产环境真实的重复提问率，现在没有这个数据，没有数据支撑"值不值得做"这个前提，不写任务。
- **`correction_check`（纠错意图检测）的前置过滤**：报告里和 `resolve_time_window` 一起提到，但两者风险不对称——"是否含时间表达"是一个有清晰关键词特征、误判代价低（`resolve_time_window` 本身对没解析出窗口的问题就是 no-op）的分类问题；"是否在纠正上一句话"是更模糊的语义判断，一个简陋的启发式规则如果假阴性漏判真实的纠错意图，会静默丢失用户的纠正，且没有兜底（不像 `resolve_time_window` 有"反正只是加成，漏了也不影响主链路"这层安全网）。这个需要先有真实对话数据统计"纠错意图占比"和"简单规则的误判率"，本计划不做。

---

## Task 1: BM25 检索移出事件循环，并与查询改写/向量检索并发

**Files:**
- Modify: `app/retrieval/hybrid_search.py:1-51` (imports + docstring), `app/retrieval/hybrid_search.py:52-93` (函数体)
- Test: `tests/retrieval/test_hybrid_search.py`

**Interfaces:**
- Consumes: `app.retrieval.bm25.BM25Index.search()`（同步方法，签名不变：`search(self, query: str, *, top_k: int, tenant_id: str) -> list[BM25Hit]`，本任务不改这个类）
- Produces: `hybrid_search()` 对外签名和返回值完全不变（`list[VectorRecord]`），只改内部执行顺序——后续任务不依赖这里的内部实现细节

`bm25_index.search()` 目前是纯同步代码，直接在 `hybrid_search()` 的 async 函数体内调用（`hybrid_search.py:84`），且排在"改写 query + 向量检索"完全跑完之后才执行——这两点合在一起既会阻塞事件循环（真实测试实测单次 3ms–308ms），又是纯粹浪费的串行等待（`bm25_search` 只依赖原始 `question`，不依赖改写结果）。

- [ ] **Step 1: 写一个会在退化回顺序执行时挂起超时的测试**

在 `tests/retrieval/test_hybrid_search.py` 文件顶部（第一行 `from app.providers.base import ...` 之前）加：

```python
import asyncio
import time

```

然后在文件末尾追加：

```python
async def test_hybrid_search_runs_bm25_concurrently_with_rewrite_and_vector_search():
    """bm25_search 只依赖原始 question，和"改写+向量检索"那条链路没有数据
    依赖——用真实耗时差证明两者是并发跑的，不是先后执行。跨线程场景（bm25
    走 asyncio.to_thread）没法用 asyncio.Event 互等做确定性证明，所以这里
    退而求其次用耗时差：如果退化回顺序执行，总耗时会 ≈ 0.4s；真正并发时
    应该 ≈ 0.2s（两段各自 sleep 0.2s，重叠执行）。
    """
    records = [
        VectorRecord(
            id="doc1", vector=[1.0, 0.0], text="错误码 E502 表示网关超时",
            tenant_id="t1", metadata={},
        ),
    ]
    store = InMemoryVectorStore()
    await store.upsert(records)

    class SlowEmbeddingProvider:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
            await asyncio.sleep(0.2)
            return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("slow-embedding", SlowEmbeddingProvider())
    llm_registry = ProviderRegistry()  # 不注册 provider，query_rewrite_enabled=False 时不会用到

    class SlowBM25Index(BM25Index):
        def search(self, query: str, *, top_k: int, tenant_id: str):
            time.sleep(0.2)
            return super().search(query, top_k=top_k, tenant_id=tenant_id)

    slow_bm25 = SlowBM25Index()
    slow_bm25.index(records)

    start = time.perf_counter()
    await hybrid_search(
        "E502 错误码是什么意思",
        embedding_registry=embedding_registry,
        embedding_provider_name="slow-embedding",
        vector_store=store,
        bm25_index=slow_bm25,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        query_rewrite_enabled=False,
        final_top_k=2,
        tenant_id="t1",
    )
    elapsed = time.perf_counter() - start

    assert elapsed < 0.35, (
        f"耗时 {elapsed:.3f}s 接近 0.4s（两段串行相加），bm25 没有和向量检索并发"
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/retrieval/test_hybrid_search.py::test_hybrid_search_runs_bm25_concurrently_with_rewrite_and_vector_search -v`
Expected: FAIL，`elapsed` 接近 0.4s（当前实现是顺序执行）

- [ ] **Step 3: 重构 `hybrid_search()`，把 bm25 检索改成 `asyncio.to_thread` 并和改写+向量检索并发**

将 `app/retrieval/hybrid_search.py` 里 `hybrid_search()` 函数体（`candidates: dict[str, VectorRecord] = {}` 开始到 `return fused_records[:final_top_k]`/rerank 分支结束）替换为：

```python
    candidates: dict[str, VectorRecord] = {}
    ranked_id_lists: list[list[str]] = []

    async def _vector_search_for_text(text: str) -> list[VectorRecord]:
        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=[text]),
            provider_name=embedding_provider_name,
        )
        return await vector_store.search(
            embed_result.vectors[0], top_k=vector_top_k, tenant_id=tenant_id
        )

    async def _rewrite_and_vector_search() -> list[list[VectorRecord]]:
        query_texts = [question]
        if query_rewrite_enabled:
            rewritten = await rewrite_query(
                question,
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                timeout_sec=query_rewrite_timeout_sec,
                conversation_context=conversation_context,
            )
            if rewritten != question:
                query_texts.append(rewritten)
        return await asyncio.gather(
            *(_vector_search_for_text(text) for text in query_texts)
        )

    async def _bm25_search() -> list:
        return await asyncio.to_thread(
            bm25_index.search, question, top_k=bm25_top_k, tenant_id=tenant_id
        )

    # bm25_search 只依赖原始 question，和"改写+向量检索"这条链路没有数据
    # 依赖，2026-08-10 起改成并发发起，不再排在改写+向量检索全部跑完之后；
    # 同时用 asyncio.to_thread 包一层——bm25 是纯同步 CPU 计算（每次对整个
    # 租户全量重算 tf/idf，没有预建倒排索引），不包线程会独占事件循环，
    # 期间同一进程收到的其它请求全部卡住排队，真实测试量过单次 3ms–308ms。
    # 用 return_exceptions=True 等两边都跑完再手动重新抛出，而不是用 gather
    # 默认行为：默认行为下一边抛异常会让 gather 立刻返回、另一边可能还在
    # 后台裸跑，产生不会被任何人处理的"未获取异常"。
    results = await asyncio.gather(
        _rewrite_and_vector_search(), _bm25_search(), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    per_text_hits, bm25_hits = results

    for vector_hits in per_text_hits:
        ranked_id_lists.append([record.id for record in vector_hits])
        for record in vector_hits:
            candidates[record.id] = record

    ranked_id_lists.append([hit.id for hit in bm25_hits])
    for hit in bm25_hits:
        candidates.setdefault(
            hit.id,
            VectorRecord(
                id=hit.id, vector=[], text=hit.text, tenant_id=tenant_id, metadata={}
            ),
        )

    fused = reciprocal_rank_fusion(*ranked_id_lists)
    fused_ids = [doc_id for doc_id, _ in fused][:fusion_top_k]
    fused_records = [candidates[doc_id] for doc_id in fused_ids if doc_id in candidates]

    if rerank_provider is None or not fused_records:
        return fused_records[:final_top_k]

    rerank_result = await rerank_provider.rerank(
        RerankRequest(
            query=question,
            documents=[record.text for record in fused_records],
            top_n=final_top_k,
        )
    )
    return [
        dataclasses.replace(fused_records[hit.index], score=hit.relevance_score)
        for hit in rerank_result.hits[:final_top_k]
    ]
```

同时把函数顶部的 docstring 最后一段（"query_texts……不需要纳入并发调度。"）替换为：

```python
    query_texts（原始问题 + 可能的改写问题）各自的向量检索用 asyncio.gather
    并发执行。bm25 检索（同步、无 IO 等待，纯 CPU 计算）用 asyncio.to_thread
    包一层，和"改写+向量检索"整条链路并发发起——bm25_search 只依赖原始
    question，和改写结果没有数据依赖，2026-08-10 起不再排在改写+向量检索
    之后串行执行；包一层线程同时避免了 bm25 的同步计算独占事件循环。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/retrieval/test_hybrid_search.py -v`
Expected: PASS（全部测试，包括新增的并发证明测试和已有的 5 个测试）

- [ ] **Step 5: 提交**

```bash
git add app/retrieval/hybrid_search.py tests/retrieval/test_hybrid_search.py
git commit -m "perf(retrieval): run bm25 search off the event loop, concurrent with query rewrite + vector search"
```

---

## Task 2: `answer_question()` 里 term_guard 和 hybrid_search 并发

**Files:**
- Modify: `app/qa/answer.py`
- Test: `tests/qa/test_answer.py`

**Interfaces:**
- Consumes: `build_term_guard_context(text, *, terms, tenant_id, graph_client) -> str | None`（`app/graphrag/term_guard.py`，签名不变）、`hybrid_search(...) -> list[VectorRecord]`（Task 1 之后签名不变）
- Produces: `answer_question()` 对外签名和返回值不变

`term_guard_context`（只依赖 `question`/`terms`/`graph_client`）和 `hybrid_search` 的结果（`records`）在 `answer_question()` 里彼此没有数据依赖——只有拼 prompt 那一步才会同时用到两者。当前是 `await` 顺序执行。

- [ ] **Step 1: 写一个 deadlock 式并发证明测试**

在 `tests/qa/test_answer.py` 文件顶部（第一行 `from app.graphrag.ontology import Term` 之前）加：

```python
import asyncio

import app.qa.answer as answer_module

```

然后在文件末尾追加：

```python
async def test_answer_question_runs_term_guard_and_hybrid_search_concurrently(monkeypatch):
    """用两个互相等待对方先启动的 asyncio.Event 证明 term_guard 和
    hybrid_search 是并发跑的：如果退化回顺序执行，先启动的一方会一直等
    不到另一方启动、卡到 asyncio.wait_for 超时，测试会失败而不是静默
    通过——比断言耗时更短更可靠。
    """
    term_guard_started = asyncio.Event()
    hybrid_search_started = asyncio.Event()

    async def fake_build_term_guard_context(question, *, terms, tenant_id, graph_client):
        term_guard_started.set()
        await asyncio.wait_for(hybrid_search_started.wait(), timeout=5)
        return "检测到专有名词：示例术语"

    async def fake_hybrid_search(question, **kwargs):
        hybrid_search_started.set()
        await asyncio.wait_for(term_guard_started.wait(), timeout=5)
        return []

    monkeypatch.setattr(answer_module, "build_term_guard_context", fake_build_term_guard_context)
    monkeypatch.setattr(answer_module, "hybrid_search", fake_hybrid_search)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=EmbeddingRegistry(),
        embedding_provider_name="fake-embedding",
        vector_store=InMemoryVectorStore(),
        bm25_index=BM25Index(),
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tenant_id="t1",
        terms=[
            Term(
                standard_name="示例术语", aliases=["示例术语"],
                term_type="module", product_line="示例产品线",
            )
        ],
        graph_client=FakeGraphClient(),
    )

    assert "检测到专有名词：示例术语" in llm_provider.requests[0].messages[0]["content"]
    assert result.text == "按资料所述，重启路由器即可解决。"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/qa/test_answer.py::test_answer_question_runs_term_guard_and_hybrid_search_concurrently -v`
Expected: FAIL，`asyncio.wait_for` 超时（当前实现是 term_guard 先完全跑完才开始 hybrid_search）

- [ ] **Step 3: 重构 `answer_question()`**

在 `app/qa/answer.py` 顶部加 `import asyncio`：

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass
```

把函数体里这一段：

```python
    term_guard_context: str | None = None
    if terms and graph_client is not None:
        term_guard_context = await build_term_guard_context(
            question, terms=terms, tenant_id=tenant_id, graph_client=graph_client
        )

    records = await hybrid_search(
        question,
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        rerank_provider=rerank_provider,
        query_rewrite_enabled=query_rewrite_enabled,
        final_top_k=top_k,
        tenant_id=tenant_id,
    )
```

替换为：

```python
    async def _maybe_term_guard() -> str | None:
        if terms and graph_client is not None:
            return await build_term_guard_context(
                question, terms=terms, tenant_id=tenant_id, graph_client=graph_client
            )
        return None

    async def _do_hybrid_search():
        return await hybrid_search(
            question,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            final_top_k=top_k,
            tenant_id=tenant_id,
        )

    # term_guard_context 只依赖 question/terms/graph_client，hybrid_search
    # 的结果只依赖 question 本身，两者互不依赖，只有拼 prompt 那一步才会
    # 同时用到——2026-08-10 起改成并发发起。return_exceptions=True 等
    # 两边都跑完再手动重新抛出：保留"term_guard 失败必须让整个问答请求
    # 失败"这条现状（改造前也是直接 await、异常直接传染），不能因为改成
    # 并发就意外吞掉。
    results = await asyncio.gather(
        _maybe_term_guard(), _do_hybrid_search(), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    term_guard_context, records = results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/qa/test_answer.py -v`
Expected: PASS（全部测试）

- [ ] **Step 5: 提交**

```bash
git add app/qa/answer.py tests/qa/test_answer.py
git commit -m "perf(qa): run term_guard and hybrid_search concurrently in answer_question"
```

---

## Task 3: `build_term_guard_context()` 内多术语查询并发化

**Files:**
- Modify: `app/graphrag/term_guard.py`
- Test: `tests/graphrag/test_term_guard.py`

**Interfaces:**
- Consumes: `GraphClientProtocol.query_subgraph(standard_name, *, tenant_id) -> list[dict]`（不变）
- Produces: `build_term_guard_context()` 对外签名和返回字符串格式完全不变——命中术语的展示顺序必须和 `matched`（`match_terms()` 的返回顺序）一致，不能因为并发查询就变成完成顺序

这个函数同时被 `app/qa/answer.py`（Task 2 已经并发化的分支）和 `app/agent/planner.py::graph_query_tool` 路径间接复用，命中多个术语时对每个术语的 `query_subgraph` 调用目前是 `for` 循环里顺序 `await`。

- [ ] **Step 1: 写一个 deadlock 式并发证明测试**

在 `tests/graphrag/test_term_guard.py` 文件顶部（第一行 `from app.graphrag.ontology import Term` 之前）加 `import asyncio`。然后在文件末尾追加：

```python
_TWO_TERMS = [
    Term(
        standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code", product_line="核心平台",
    ),
    Term(
        standard_name="登录模块", aliases=["登录失败"],
        term_type="module", product_line="核心平台",
    ),
]


async def test_build_term_guard_context_queries_multiple_matched_terms_concurrently():
    """命中两个术语时，两次 query_subgraph 调用应该并发发起，不是排队
    顺序执行——用两个互等的 asyncio.Event 证明，退化回顺序执行会卡到
    超时。"""
    started = {"错误码E502": asyncio.Event(), "登录模块": asyncio.Event()}

    class SyncGraphClient:
        async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
            started[standard_name].set()
            other = "登录模块" if standard_name == "错误码E502" else "错误码E502"
            await asyncio.wait_for(started[other].wait(), timeout=5)
            return [{"related_name": f"{standard_name}关联项", "relation_type": "RELATED_TO"}]

    context = await build_term_guard_context(
        "网关超时导致登录失败", terms=_TWO_TERMS, tenant_id="t1",
        graph_client=SyncGraphClient(),
    )

    # 展示顺序必须按 matched（即 terms 表里的原始顺序）排列，不能因为
    # 并发查询导致谁先完成谁排前面。
    assert context.index("错误码E502") < context.index("登录模块")
    assert "错误码E502关联项" in context
    assert "登录模块关联项" in context
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/graphrag/test_term_guard.py::test_build_term_guard_context_queries_multiple_matched_terms_concurrently -v`
Expected: FAIL，`asyncio.wait_for` 超时（当前实现是 `for` 循环顺序 `await`）

- [ ] **Step 3: 重构 `build_term_guard_context()`**

把 `app/graphrag/term_guard.py` 里这一段：

```python
    lines = ["检测到以下专有名词，已强制注入知识图谱上下文（回答时请使用标准名称）："]
    for term in matched:
        lines.append(
            f"- {term.standard_name}（类型: {term.term_type}, 产品线: {term.product_line}）"
        )
        subgraph = await graph_client.query_subgraph(
            term.standard_name, tenant_id=tenant_id
        )
        for row in subgraph:
            hops = row.get("hops", 1)
            label = describe_association(hops)
            lines.append(
                f"  {label}: {row['related_name']}（关系: {row['relation_type']}）"
            )
    return "\n".join(lines)
```

替换为：

```python
    # 命中多个术语时，每个术语各自的 query_subgraph 调用彼此没有数据
    # 依赖——2026-08-10 起改成 asyncio.gather 并发查询，不再是 for 循环
    # 顺序 await。展示顺序仍然严格按 matched（术语表原始顺序）排列，不
    # 按查询完成的先后顺序，靠先 gather 再按索引组装文本实现，不是靠
    # "谁先跑完谁先加进 lines"。
    async def _query_one(term: Term) -> list[dict[str, Any]]:
        return await graph_client.query_subgraph(term.standard_name, tenant_id=tenant_id)

    subgraphs = await asyncio.gather(*(_query_one(term) for term in matched))

    lines = ["检测到以下专有名词，已强制注入知识图谱上下文（回答时请使用标准名称）："]
    for term, subgraph in zip(matched, subgraphs):
        lines.append(
            f"- {term.standard_name}（类型: {term.term_type}, 产品线: {term.product_line}）"
        )
        for row in subgraph:
            hops = row.get("hops", 1)
            label = describe_association(hops)
            lines.append(
                f"  {label}: {row['related_name']}（关系: {row['relation_type']}）"
            )
    return "\n".join(lines)
```

在文件顶部加 `import asyncio`：

```python
from __future__ import annotations

import asyncio
from typing import Any, Protocol
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/graphrag/test_term_guard.py -v`
Expected: PASS（全部测试，包括已有的顺序/两跳标注测试——顺序断言在这个改动下依然成立）

- [ ] **Step 5: 提交**

```bash
git add app/graphrag/term_guard.py tests/graphrag/test_term_guard.py
git commit -m "perf(graphrag): query multiple matched terms' subgraphs concurrently in term_guard"
```

---

## Task 4: `run_tool_calls()` 内多工具调用并发化

**Files:**
- Modify: `app/agent/planner.py:140-199`
- Test: `tests/agent/test_planner.py`

**Interfaces:**
- Consumes: `_dispatch_tool_call(name, arguments, *, tenant_id, embedding_registry, embedding_provider_name, vector_store, bm25_index, llm_registry, llm_provider_name, rerank_provider, query_rewrite_enabled, terms, graph_client) -> tuple[str, list[VectorRecord]]`（不变，`planner.py:80-137`）
- Produces: `run_tool_calls()` 返回的 `dict` 结构不变；`tool_results`/`messages` 里每条工具调用结果的顺序必须和 `state["pending_tool_calls"]` 的原始顺序一致（LLM 后续推理依赖 `tool_call_id` 对应关系，不依赖顺序本身，但保持顺序确定性便于排查）

LLM 同一轮如果请求了多个工具（比如同时要 `vector_search_tool` 和 `graph_query_tool`），当前是 `for` 循环顺序 `await` 执行。

- [ ] **Step 1: 写一个 deadlock 式并发证明测试**

在 `tests/agent/test_planner.py` 文件顶部（第一行 `import json` 之后）加 `import asyncio`。然后在文件末尾追加：

```python
async def test_run_tool_calls_executes_multiple_tools_concurrently(monkeypatch):
    """同一轮请求了两个工具时，两次 _dispatch_tool_call 应该并发执行，
    不是排队顺序执行——用两个互等的 asyncio.Event 证明。"""
    import app.agent.planner as planner_module

    started = {"call_1": asyncio.Event(), "call_2": asyncio.Event()}

    async def fake_dispatch_tool_call(name, arguments, **kwargs):
        call_id = arguments["call_id"]
        started[call_id].set()
        other = "call_2" if call_id == "call_1" else "call_1"
        await asyncio.wait_for(started[other].wait(), timeout=5)
        return f'{{"ok": "{call_id}"}}', []

    monkeypatch.setattr(planner_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    state = {
        "tenant_id": "t1",
        "planner_messages": [],
        "pending_tool_calls": [
            {"id": "call_1", "name": "vector_search_tool", "arguments": '{"call_id": "call_1"}'},
            {"id": "call_2", "name": "graph_query_tool", "arguments": '{"call_id": "call_2"}'},
        ],
    }

    update = await run_tool_calls(
        state,
        embedding_registry=_embedding_registry(),
        embedding_provider_name="fake-embedding",
        vector_store=InMemoryVectorStore(),
        bm25_index=BM25Index(),
        llm_registry=ProviderRegistry(),
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    contents_by_call_id = {r["tool_call_id"]: r["content"] for r in update["tool_results"]}
    assert contents_by_call_id["call_1"] == '{"ok": "call_1"}'
    assert contents_by_call_id["call_2"] == '{"ok": "call_2"}'
    # 顺序必须和 pending_tool_calls 原始顺序一致，不依赖谁先完成
    assert [r["tool_call_id"] for r in update["tool_results"]] == ["call_1", "call_2"]
```

需要在文件顶部补 `from app.retrieval.vector_store import InMemoryVectorStore`（如果尚未导入）。

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_planner.py::test_run_tool_calls_executes_multiple_tools_concurrently -v`
Expected: FAIL，`asyncio.wait_for` 超时（当前实现是 `for` 循环顺序 `await`）

- [ ] **Step 3: 重构 `run_tool_calls()`**

把 `app/agent/planner.py` 里 `run_tool_calls()` 函数体（从 `tenant_id = state["tenant_id"]` 到 `return {...}`）替换为：

```python
    tenant_id = state["tenant_id"]
    messages = list(state.get("planner_messages", []))
    retrieved_records = list(state.get("retrieved_records", []))
    tool_results = list(state.get("tool_results", []))
    pending_calls = state.get("pending_tool_calls", [])

    async def _execute_one(call: dict[str, Any]) -> tuple[dict, list[VectorRecord]]:
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            content = json.dumps({"error": "arguments 不是合法 JSON"}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        content, new_records = await _dispatch_tool_call(
            call["name"],
            arguments,
            tenant_id=tenant_id,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
        )
        return (
            {"tool_call_id": call["id"], "name": call["name"], "content": content},
            new_records,
        )

    # 同一轮 LLM 可能同时请求多个工具（比如 vector_search_tool +
    # graph_query_tool），彼此没有数据依赖——2026-08-10 起改成
    # asyncio.gather 并发执行，不再是 for 循环顺序 await。结果顺序按
    # pending_calls 原始顺序组装（asyncio.gather 保证返回顺序和传入协程
    # 顺序一致，不按完成先后），不因为改成并发就打乱 tool_call_id 对应
    # 关系的可读性。
    outcomes = await asyncio.gather(*(_execute_one(call) for call in pending_calls))

    for tool_result, new_records in outcomes:
        existing_ids = {r.id for r in retrieved_records}
        retrieved_records.extend(r for r in new_records if r.id not in existing_ids)
        tool_results.append(tool_result)
        messages.append({"role": "tool", "tool_call_id": tool_result["tool_call_id"], "content": tool_result["content"]})

    return {
        "planner_messages": messages,
        "pending_tool_calls": [],
        "retrieved_records": retrieved_records,
        "used_sources": [r.id for r in retrieved_records],
        "tool_results": tool_results,
        "tool_call_round": state.get("tool_call_round", 0) + 1,
    }
```

保留原函数开头的 docstring 不变；在文件顶部确认已有 `import asyncio`（`run_planner_turn` 所在文件目前没有 import asyncio，需要新增）：

```python
from __future__ import annotations

import asyncio
import json
from typing import Any
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_planner.py -v`
Expected: PASS（全部测试，包括已有的错误参数/单工具/两跳标注等测试）

- [ ] **Step 5: 提交**

```bash
git add app/agent/planner.py tests/agent/test_planner.py
git commit -m "perf(agent): execute multiple tool calls concurrently in run_tool_calls"
```

---

## Task 5: `resolve_time_window` 增加规则前置过滤，跳过明显无时间表达的消息

**Files:**
- Create: 无新文件，在 `app/agent/graph.py` 内新增一个模块级纯函数
- Modify: `app/agent/graph.py:279-323`（`memory_recall_node`）
- Test: `tests/agent/test_graph.py`, `tests/agent/test_graph_memory.py`（修正因调用次数变化而失效的既有测试）

**Interfaces:**
- Consumes: 无新依赖
- Produces: 新增纯函数 `_looks_temporal(text: str) -> bool`（模块内部，不导出）；`memory_recall_node` 对外行为不变——只是对"明显不含时间表达"的问题跳过 `resolve_time_window` 的 LLM 调用，直接构造 `resolved=False` 的结果

`resolve_time_window()`（`app/memory/temporal_resolver.py`）本身不改——它的单元测试（`tests/memory/test_temporal_resolver.py`）和另一个调用方（`app/memory/consolidation_worker.py` 用于判断"延迟跟进"的时间，走 `is_delay=true` 时才触发，本来就不是每条消息都跑）保持完全不受影响。过滤只加在 `memory_recall_node` 这一个调用点，因为报告里点名的问题就是"每条消息都无条件触发"。

**⚠️ 这个任务会改变 `memory_recall_node` 在"问题不含时间表达"时的 LLM 调用次数**（从 1 次变成 0 次）。已经用真实文本核对过，`tests/agent/test_graph_memory.py` 里以下 5 处用 `"网络连不上怎么办？"` 或 `"这个报错怎么解决"` 提问、且脚本了一条 `resolve_time_window` 低置信度响应的测试会受影响：第 103、152、203、249、378 行；其中第 131 行和第 404 行还各有一处 `llm_provider.requests[N]` 的下标断言需要跟着往前移一位。`tests/agent/test_graph.py:313` 用的是 `"上周三"`（含"周"字，命中过滤规则，仍然会走 LLM）和 `tests/memory/test_consolidation.py:163`（走的是 consolidation worker 那个不同的调用点，本任务不碰）都不受影响，不用改。

- [ ] **Step 1: 给新的纯函数写失败测试**

```python
# tests/agent/test_graph.py 顶部 import 区加一行
from app.agent.graph import _looks_temporal


# 文件末尾追加
def test_looks_temporal_detects_common_chinese_time_expressions():
    assert _looks_temporal("昨天那个E502问题解决了吗") is True
    assert _looks_temporal("上周三提交的工单处理了吗") is True
    assert _looks_temporal("2025年3月15号的账单在哪") is True
    assert _looks_temporal("刚才说的那个方案") is True


def test_looks_temporal_returns_false_for_questions_without_time_cues():
    assert _looks_temporal("网络连不上怎么办？") is False
    assert _looks_temporal("这个报错怎么解决") is False
    assert _looks_temporal("错误码E502网关超时怎么解决") is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_graph.py::test_looks_temporal_detects_common_chinese_time_expressions -v`
Expected: FAIL，`ImportError: cannot import name '_looks_temporal'`

- [ ] **Step 3: 实现 `_looks_temporal()` 并接入 `memory_recall_node`**

在 `app/agent/graph.py` 顶部 `import` 区加 `import re`（如果尚未导入——当前文件没有 `re`，需要新增），并在 `_PLANNER_SYSTEM_PROMPT` 常量定义之后加：

```python
# resolve_time_window 是一次 LLM 调用，之前对每条消息（不管有没有时间
# 表达）都无条件触发——2026-08-10 真实压测发现这是问答链路里"能跳过就
# 跳过"的典型例子。这里的正则故意设计得宽松（宁可漏判"跳过"、不能漏判
# "应该走 LLM"）：只有在完全匹配不到任何时间线索时才跳过 LLM 调用；
# resolve_time_window 本身对"没有真正解析出时间窗口"的问题就是 no-op
# （见 memory_recall_node 的说明），所以这里即使漏判了某个生僻的时间
# 表达没拦下来，最坏结果也只是少了一次本来就是可选加成的结构化历史检索，
# 不会影响主回答链路。
_TEMPORAL_CUE_PATTERN = re.compile(
    r"[今昨前后明][天年月日]|[上下]午|[上下]周|星期|周[一二三四五六天日]|"
    r"\d+\s*(年|月|日|号|点|时|分钟|小时|天前|天后|周前|周后)|"
    r"刚才|最近|之前|以前|去年|今年|明年|早上|晚上|凌晨|中午|傍晚"
)


def _looks_temporal(text: str) -> bool:
    return bool(_TEMPORAL_CUE_PATTERN.search(text))
```

把 `memory_recall_node` 里这一段：

```python
        time_result = await resolve_time_window(
            state["question"],
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            reference_time=datetime.now(),
        )
```

替换为：

```python
        if _looks_temporal(state["question"]):
            time_result = await resolve_time_window(
                state["question"],
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                reference_time=datetime.now(),
            )
        else:
            time_result = TimeWindowResult(
                resolved=False, start=None, end=None, confidence=0.0,
                is_future=False, source="unresolved",
            )
```

在文件顶部 import 区把：

```python
from app.memory.temporal_resolver import resolve_time_window
```

改成：

```python
from app.memory.temporal_resolver import TimeWindowResult, resolve_time_window
```

- [ ] **Step 4: 运行新增测试确认通过**

Run: `python -m pytest tests/agent/test_graph.py::test_looks_temporal_detects_common_chinese_time_expressions tests/agent/test_graph.py::test_looks_temporal_returns_false_for_questions_without_time_cues -v`
Expected: PASS

- [ ] **Step 5: 运行受影响的既有测试文件，定位因调用次数变化而失败的用例**

Run: `python -m pytest tests/agent/test_graph_memory.py -v`
Expected: 多个测试因为 `ScriptedLLMProvider` 的响应队列比实际 LLM 调用次数多一条而报错（`IndexError` 或断言错位）

- [ ] **Step 6: 修正 `tests/agent/test_graph_memory.py` 里 5 处受影响的响应队列**

以下 5 处，删掉这一行（保留其它响应，紧邻的上下行不变）：

```python
                '{"start": null, "end": null, "confidence": 0}',  # memory_recall_node 的 resolve_time_window，问题里没有时间表达式
```

对应位置（删除前的原始行号，实际操作时用编辑器搜索这行文本定位，不用死记行号）：
1. `test_query_rewrite_receives_recent_conversation_turns_as_context`（约第 103 行）—— 删除后，同一个测试里第 131 行的 `rewrite_request = llm_provider.requests[2]` 要改成 `llm_provider.requests[1]`（原顺序是 `[0]=correction_check, [1]=resolve_time_window(删除), [2]=rewrite_query` → 删除后变成 `[0]=correction_check, [1]=rewrite_query`）。
2. `test_memory_enabled_saves_turn_and_injects_context`（约第 152 行）—— 无下标断言需要跟着改。
3. `test_memory_enabled_enqueues_consolidation_job_without_blocking_response`（约第 203 行）—— 同时把第 229-231 行的注释"上面只准备了纠错意图检测+resolve_time_window+responder+语义审查四个脚本响应"改成"上面只准备了纠错意图检测+responder+语义审查三个脚本响应"，保持注释和代码一致。
4. `test_memory_enabled_stores_embedding_for_newly_added_facts_after_worker_drains_queue`（约第 249 行）—— 无下标断言需要跟着改。
5. `test_memory_recall_stays_noop_when_question_has_no_time_expression`（约第 378 行）—— 删除后，第 404 行的 `responder_request = llm_provider.requests[2]` 要改成 `llm_provider.requests[1]`（原顺序 `[0]=correction_check, [1]=resolve_time_window(删除), [2]=responder` → 删除后 `[0]=correction_check, [1]=responder`）。这个测试改完之后其实更直接地证明了"无时间表达式=完全 no-op"——现在连 LLM 调用本身都不会发生，不再需要靠脚本一个低置信度响应来间接验证。

- [ ] **Step 7: 运行完整的受影响测试文件确认全部通过**

Run: `python -m pytest tests/agent/test_graph.py tests/agent/test_graph_memory.py tests/memory/test_consolidation.py -v`
Expected: PASS（全部测试；`test_consolidation.py` 不需要改动，此处运行只是确认没有被意外影响）

- [ ] **Step 8: 提交**

```bash
git add app/agent/graph.py tests/agent/test_graph.py tests/agent/test_graph_memory.py
git commit -m "perf(agent): skip resolve_time_window's LLM call when the question has no temporal cue"
```

---

## Task 6: `parse_pdf()` 支持注入共享的 OCR/表格提取 Semaphore

**Files:**
- Modify: `app/ingestion/pdf_parser.py:291-424`（`parse_pdf()` 签名 + 两个内部阶段函数）
- Test: `tests/ingestion/test_pdf_parser.py`

**Interfaces:**
- Consumes: 无新依赖
- Produces: `parse_pdf()` 新增两个可选参数 `ocr_semaphore: asyncio.Semaphore | None = None`、`table_semaphore: asyncio.Semaphore | None = None`——不传（默认）时行为和现在完全一致（内部各自新建一个 Semaphore）；传入时用调用方给的共享 Semaphore，供 Task 7 在跨文档并发场景下正确共享账号级并发预算

这一步本身不改变任何默认行为，只是打开一个"调用方可以注入共享 Semaphore"的口子，是 Task 7 的前置依赖。

- [ ] **Step 1: 写一个证明"传入的 Semaphore 真的被使用"的测试**

`tests/ingestion/test_pdf_parser.py` 顶部已经有 `import asyncio`（本会话之前的并发测试引入的），不需要再加。在文件末尾追加：

```python
async def test_parse_pdf_uses_injected_ocr_semaphore_instead_of_building_its_own(tmp_path):
    """传入 ocr_semaphore 时，OCR 阶段应该受这个共享 Semaphore 的并发上限
    约束，而不是内部按 max_concurrency 另建一个——用并发上限为 1 的共享
    Semaphore + 两个会记录"当前同时在执行的调用数"的 OCR 请求验证。
    """
    pdf_path = tmp_path / "scanned_multi.pdf"
    _write_image_only_pdf(pdf_path, tmp_path, pages=2)

    shared_semaphore = asyncio.Semaphore(1)
    concurrent_count = {"current": 0, "max_seen": 0}

    async def tracking_ocr(path):
        concurrent_count["current"] += 1
        concurrent_count["max_seen"] = max(concurrent_count["max_seen"], concurrent_count["current"])
        await asyncio.sleep(0.05)
        concurrent_count["current"] -= 1
        return "文字"

    await parse_pdf(
        pdf_path,
        ocr=tracking_ocr,
        max_concurrency=8,  # 内部默认并发上限调高，验证真正生效的是注入的共享 Semaphore（上限1）
        ocr_semaphore=shared_semaphore,
    )

    assert concurrent_count["max_seen"] == 1, (
        f"实际同时并发数 {concurrent_count['max_seen']}，应该被注入的共享 Semaphore(1) 限制住，"
        "而不是用了内部按 max_concurrency=8 新建的 Semaphore"
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/ingestion/test_pdf_parser.py::test_parse_pdf_uses_injected_ocr_semaphore_instead_of_building_its_own -v`
Expected: FAIL，`TypeError: parse_pdf() got an unexpected keyword argument 'ocr_semaphore'`

- [ ] **Step 3: 修改 `parse_pdf()` 签名和两个内部阶段函数**

把签名：

```python
async def parse_pdf(
    path: Path,
    *,
    ocr: OcrFunction | None = None,
    render_dpi: int = _DEFAULT_OCR_RENDER_DPI,
    max_concurrency: int = _DEFAULT_OCR_MAX_CONCURRENCY,
    table_extractor: TableExtractionFunction | None = None,
    table_extraction_max_concurrency: int = _DEFAULT_TABLE_EXTRACTION_MAX_CONCURRENCY,
) -> list[Chunk]:
```

改成：

```python
async def parse_pdf(
    path: Path,
    *,
    ocr: OcrFunction | None = None,
    render_dpi: int = _DEFAULT_OCR_RENDER_DPI,
    max_concurrency: int = _DEFAULT_OCR_MAX_CONCURRENCY,
    table_extractor: TableExtractionFunction | None = None,
    table_extraction_max_concurrency: int = _DEFAULT_TABLE_EXTRACTION_MAX_CONCURRENCY,
    ocr_semaphore: asyncio.Semaphore | None = None,
    table_semaphore: asyncio.Semaphore | None = None,
) -> list[Chunk]:
```

在 docstring 末尾（"...报一个不会被任何人处理的'未获取异常'。"之后）加一段：

```python

    ocr_semaphore/table_semaphore 为可选项：不传（默认）时每次调用各自
    新建一个 Semaphore，行为和之前完全一致（单文档内部按 max_concurrency/
    table_extraction_max_concurrency 限并发）。跨文档批量摄取场景下，
    process_pending_jobs() 会在处理一批任务之前构造好这两个 Semaphore
    并注入给每一份文档的 parse_pdf() 调用，让多份文档共享同一个账号级
    并发预算，而不是每份文档各自独立地把并发数用满——见
    docs/superpowers/plans/2026-08-10-qa-and-ingestion-concurrency-
    optimization.md。
    """
```

（注意：上面这段接在原有 docstring 的三引号闭合之前，不要重复写闭合三引号。）

把 `_run_ocr_phase` 内的：

```python
        async def _run_ocr_phase() -> None:
            assert ocr is not None  # needs_ocr=True 时才会有非空 ocr_page_indexes
            ocr_semaphore = asyncio.Semaphore(max_concurrency)

            async def _bounded_ocr(page_index: int) -> tuple[int, str]:
                async with ocr_semaphore:
```

改成：

```python
        async def _run_ocr_phase() -> None:
            assert ocr is not None  # needs_ocr=True 时才会有非空 ocr_page_indexes
            semaphore = ocr_semaphore or asyncio.Semaphore(max_concurrency)

            async def _bounded_ocr(page_index: int) -> tuple[int, str]:
                async with semaphore:
```

同样把 `_run_table_extraction_phase` 内的：

```python
        async def _run_table_extraction_phase() -> None:
            assert table_extractor is not None
            table_semaphore = asyncio.Semaphore(table_extraction_max_concurrency)

            async def _bounded_table_extract(
                page_index: int,
            ) -> tuple[int, list[str] | BaseException]:
                async with table_semaphore:
```

改成：

```python
        async def _run_table_extraction_phase() -> None:
            assert table_extractor is not None
            semaphore = table_semaphore or asyncio.Semaphore(table_extraction_max_concurrency)

            async def _bounded_table_extract(
                page_index: int,
            ) -> tuple[int, list[str] | BaseException]:
                async with semaphore:
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/ingestion/test_pdf_parser.py -v`
Expected: PASS（全部测试，包括已有的所有 OCR/表格提取相关测试——不传新参数时行为不变）

- [ ] **Step 5: 提交**

```bash
git add app/ingestion/pdf_parser.py tests/ingestion/test_pdf_parser.py
git commit -m "feat(ingestion): let parse_pdf accept an injected shared semaphore for OCR/table extraction"
```

---

## Task 7: `process_pending_jobs()` 跨文档并发调度

**Files:**
- Modify: `app/ingestion/ingestion_queue.py:163-282`（`_parse_file()` + `process_pending_jobs()`）
- Modify: `app/config/settings.py`（新增 `ingestion_job_concurrency` 配置项）
- Modify: `app/api/admin_document_routes.py`（把新配置项接到网页上传的后台任务）
- Modify: `app/ingestion/incremental_main.py`（把新配置项接到 CLI 增量摄取脚本）
- Test: `tests/ingestion/test_ingestion_queue.py`

**Interfaces:**
- Consumes: Task 6 产出的 `parse_pdf(..., ocr_semaphore=..., table_semaphore=...)`
- Produces: `process_pending_jobs()` 新增可选参数 `job_concurrency: int = 1`（默认 1 = 和现状完全一致的严格串行）；`_parse_file()` 新增可选参数 `ocr_semaphore`/`table_semaphore`，透传给 `parse_pdf()`；`Settings.ingestion_job_concurrency` 经 `admin_document_routes.py::_run_pending_jobs`/`incremental_main.py::main` 两条生产路径实际接到 `process_pending_jobs(job_concurrency=...)`，不是定义了却没人读的死配置

默认值刻意保守（`job_concurrency=1`），提高这个值之前需要先用本会话已经反复验证过的方法（同一批真实文档、控制变量对比不同并发数下账号是否触发限流）实测多文档同时摄取时 OCR/表格提取/Embedding 账号的真实承受能力——之前所有并发梯度实测都是"单文档内部多页并发"，没有测过"多文档同时发起"这种叠加负载。

- [ ] **Step 1: 给 `Settings` 加新配置项**

在 `app/config/settings.py` 的 `table_extraction_max_concurrency` 字段定义之后加：

```python
    # 摄取任务队列跨文档并发数。默认 1（严格串行，和这次改造前完全一致）
    # ——提高这个值前必须先用同一份文档批量、控制变量对比不同并发数的
    # 方法（本仓库 ocr_max_concurrency/table_extraction_max_concurrency
    # 都是这么定下来的）实测多文档同时摄取时账号的真实承受能力：之前的
    # 并发梯度实测都是单文档内部多页并发，没有测过多文档同时发起 OCR/
    # 表格提取请求这种叠加负载。见 docs/superpowers/plans/2026-08-10-
    # qa-and-ingestion-concurrency-optimization.md。
    ingestion_job_concurrency: int = 1
```

- [ ] **Step 2: 写一个 deadlock 式并发证明测试**

在 `tests/ingestion/test_ingestion_queue.py` 文件顶部（第一行 `import aiosqlite` 之后）加 `import asyncio`。然后在文件末尾追加：

```python
async def test_process_pending_jobs_processes_documents_concurrently_when_job_concurrency_above_one(tmp_path):
    """job_concurrency=2 时，两份 markdown 文档的摄取应该并发跑，不是排队
    顺序执行——用两次 embedding 调用互等的方式证明：如果退化回顺序执行，
    第一份文档的 embedding 调用会一直等不到第二份文档的 embedding 调用
    启动，卡到 asyncio.wait_for 超时。
    """
    conn = await _connect()
    (tmp_path / "a.md").write_text("## 主题A\n内容A。\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("## 主题B\n内容B。\n", encoding="utf-8")
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(tmp_path / "a.md"),
        content_hash="hash-a", action="ingest",
    )
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(tmp_path / "b.md"),
        content_hash="hash-b", action="ingest",
    )

    started_count = {"n": 0}
    both_started = asyncio.Event()

    class SyncEmbeddingProvider:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
            started_count["n"] += 1
            if started_count["n"] >= 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", SyncEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    processed = await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        job_concurrency=2,
    )

    assert processed == 2
```

需要在文件顶部补 `from app.providers.embedding import EmbeddingRegistry`（如果尚未导入完整，检查现有 import 是否已含 `EmbeddingRequest, EmbeddingResult`）。

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/ingestion/test_ingestion_queue.py::test_process_pending_jobs_processes_documents_concurrently_when_job_concurrency_above_one -v`
Expected: FAIL，`TypeError: process_pending_jobs() got an unexpected keyword argument 'job_concurrency'`

- [ ] **Step 4: 修改 `_parse_file()` 签名**

把：

```python
async def _parse_file(
    path: Path,
    *,
    ocr: OcrFunction | None,
    ocr_render_dpi: int,
    ocr_max_concurrency: int,
    table_extractor: TableExtractionFunction | None,
    table_extraction_max_concurrency: int,
):
```

改成：

```python
async def _parse_file(
    path: Path,
    *,
    ocr: OcrFunction | None,
    ocr_render_dpi: int,
    ocr_max_concurrency: int,
    table_extractor: TableExtractionFunction | None,
    table_extraction_max_concurrency: int,
    ocr_semaphore: asyncio.Semaphore | None = None,
    table_semaphore: asyncio.Semaphore | None = None,
):
```

把函数体里：

```python
    if suffix == ".pdf":
        return await parse_pdf(
            path,
            ocr=ocr,
            render_dpi=ocr_render_dpi,
            max_concurrency=ocr_max_concurrency,
            table_extractor=table_extractor,
            table_extraction_max_concurrency=table_extraction_max_concurrency,
        )
```

改成：

```python
    if suffix == ".pdf":
        return await parse_pdf(
            path,
            ocr=ocr,
            render_dpi=ocr_render_dpi,
            max_concurrency=ocr_max_concurrency,
            table_extractor=table_extractor,
            table_extraction_max_concurrency=table_extraction_max_concurrency,
            ocr_semaphore=ocr_semaphore,
            table_semaphore=table_semaphore,
        )
```

- [ ] **Step 5: 重构 `process_pending_jobs()`**

把整个函数体（从 `jobs = await list_pending_jobs(conn, limit=limit)` 到 `return processed`）替换为：

```python
    jobs = await list_pending_jobs(conn, limit=limit)

    # OCR/表格提取的 Semaphore 只在这一批任务里构造一次、跨文档共享——
    # 不这样做的话，job_concurrency>1 时每份文档各自在 parse_pdf() 内部
    # 新建一个 Semaphore，等于每份文档独立拥有一份"满额"的账号并发预算，
    # 多文档同时摄取会共同把真实请求数顶到远超账号承受能力，而不是像
    # 现在这样按配置的上限受控地共享。
    ocr_semaphore = asyncio.Semaphore(ocr_max_concurrency) if ocr is not None else None
    table_semaphore = (
        asyncio.Semaphore(table_extraction_max_concurrency)
        if table_extractor is not None
        else None
    )
    # job_concurrency 控制"同时有几份文档在处理"，默认 1 和改造前完全
    # 一致；调大之前需要先实测多文档同时摄取时账号的真实承受能力（见
    # Settings.ingestion_job_concurrency 的说明）。
    job_semaphore = asyncio.Semaphore(job_concurrency)

    async def _process_one_job(job: dict[str, Any]) -> bool:
        async with job_semaphore:
            tenant_id = job["tenant_id"]
            file_path = job["file_path"]
            try:
                await vector_store.delete_by_source(source=file_path, tenant_id=tenant_id)
                if job["action"] == "delete":
                    await remove_tracked_file(
                        conn, tenant_id=tenant_id, file_path=file_path
                    )
                else:
                    chunks = await _parse_file(
                        Path(file_path),
                        ocr=ocr,
                        ocr_render_dpi=ocr_render_dpi,
                        ocr_max_concurrency=ocr_max_concurrency,
                        table_extractor=table_extractor,
                        table_extraction_max_concurrency=table_extraction_max_concurrency,
                        ocr_semaphore=ocr_semaphore,
                        table_semaphore=table_semaphore,
                    )
                    use_graph = bool(job["build_graph"])
                    chunk_count = await _ingest_chunks(
                        chunks,
                        Path(file_path),
                        embedding_registry=embedding_registry,
                        embedding_provider_name=embedding_provider_name,
                        vector_store=vector_store,
                        tenant_id=tenant_id,
                        graph_llm_registry=graph_llm_registry if use_graph else None,
                        graph_llm_provider_name=graph_llm_provider_name if use_graph else None,
                        graph_terms=graph_terms if use_graph else None,
                        graph_client=graph_client if use_graph else None,
                        graph_review_conn=graph_review_conn if use_graph else None,
                    )
                    await record_ingested(
                        conn,
                        tenant_id=tenant_id,
                        file_path=file_path,
                        content_hash=job["content_hash"],
                        chunk_count=chunk_count,
                    )
            except Exception as exc:  # noqa: BLE001 - 任何异常都要落到重试/死信逻辑
                await mark_job_failed(
                    conn, job["job_id"], error=str(exc), max_attempts=max_attempts
                )
                return False
            await mark_job_completed(conn, job["job_id"])
            return True

    # job_concurrency=1（默认）时，job_semaphore 让这里退化成事实上的
    # 逐个处理，行为和改造前完全一致；调大之后才会真的并发跑多份文档，
    # 每个任务的失败依然单独捕获、单独判定重试/死信，不会因为一条任务
    # 出错影响同批次其它任务（try/except 在 _process_one_job 内部，不
    # 会让 gather 提前中断）。
    results = await asyncio.gather(*(_process_one_job(job) for job in jobs))
    return sum(1 for ok in results if ok)
```

把 `process_pending_jobs()` 的签名加一个新参数（在 `limit: int = 10,` 之前）：

```python
    table_extraction_max_concurrency: int = _DEFAULT_TABLE_EXTRACTION_MAX_CONCURRENCY,
    job_concurrency: int = 1,
    limit: int = 10,
```

docstring 里"每个任务的失败都单独捕获……"那句之后加一句：

```python
    job_concurrency 控制这一批任务里最多同时处理几份文档，默认 1（严格
    串行，和这次改造前完全一致）——见 Settings.ingestion_job_concurrency
    的说明，提高这个值前必须先做真实的多文档并发负载测试。
```

- [ ] **Step 6: 把 `settings.ingestion_job_concurrency` 接到网页上传路径（`admin_document_routes.py`）**

不这么接的话，Step 1 新增的 Settings 字段没有任何调用方读取它，配置了也不生效。把 `app/api/admin_document_routes.py` 里 `_run_pending_jobs()` 的签名：

```python
async def _run_pending_jobs(
    ingestion_conn: aiosqlite.Connection,
    embedding_registry: EmbeddingRegistry,
    vector_store: VectorStore,
    graph_llm_registry: ProviderRegistry,
    graph_terms: list[Term],
    graph_client: Neo4jGraphClient,
    graph_review_conn: aiosqlite.Connection | None,
    ocr: OcrFunction | None,
    ocr_render_dpi: int,
    ocr_max_concurrency: int,
    table_extractor: TableExtractionFunction | None,
    table_extraction_max_concurrency: int,
) -> None:
```

改成：

```python
async def _run_pending_jobs(
    ingestion_conn: aiosqlite.Connection,
    embedding_registry: EmbeddingRegistry,
    vector_store: VectorStore,
    graph_llm_registry: ProviderRegistry,
    graph_terms: list[Term],
    graph_client: Neo4jGraphClient,
    graph_review_conn: aiosqlite.Connection | None,
    ocr: OcrFunction | None,
    ocr_render_dpi: int,
    ocr_max_concurrency: int,
    table_extractor: TableExtractionFunction | None,
    table_extraction_max_concurrency: int,
    job_concurrency: int,
) -> None:
```

函数体里的 `await process_pending_jobs(...)` 调用加一个参数：

```python
    await process_pending_jobs(
        ingestion_conn,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
        graph_llm_registry=graph_llm_registry,
        graph_llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        graph_terms=graph_terms,
        graph_client=graph_client,
        graph_review_conn=graph_review_conn,
        ocr=ocr,
        ocr_render_dpi=ocr_render_dpi,
        ocr_max_concurrency=ocr_max_concurrency,
        table_extractor=table_extractor,
        table_extraction_max_concurrency=table_extraction_max_concurrency,
        job_concurrency=job_concurrency,
    )
```

`upload_document()` 里的 `background_tasks.add_task(...)` 调用（末尾追加一个参数）：

```python
    background_tasks.add_task(
        _run_pending_jobs,
        ingestion_conn,
        embedding_registry,
        vector_store,
        llm_registry,
        terms,
        graph_client,
        review_conn,
        ocr,
        settings.ocr_render_dpi,
        settings.ocr_max_concurrency,
        table_extractor,
        settings.table_extraction_max_concurrency,
        settings.ingestion_job_concurrency,
    )
```

- [ ] **Step 7: 把 `settings.ingestion_job_concurrency` 接到 CLI 增量摄取脚本（`incremental_main.py`）**

把 `app/ingestion/incremental_main.py` 里的：

```python
    processed = await process_pending_jobs(
        conn,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
        graph_llm_registry=resolved_graph_llm_registry,
        graph_llm_provider_name=DEFAULT_LLM_PROVIDER_NAME if build_graph else None,
        graph_terms=resolved_graph_terms,
        graph_client=resolved_graph_client,
        graph_review_conn=resolved_graph_review_conn,
        ocr=resolved_ocr,
        ocr_render_dpi=resolved_settings.ocr_render_dpi,
        ocr_max_concurrency=resolved_settings.ocr_max_concurrency,
        table_extractor=resolved_table_extractor,
        table_extraction_max_concurrency=resolved_settings.table_extraction_max_concurrency,
        limit=limit,
    )
```

改成：

```python
    processed = await process_pending_jobs(
        conn,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
        graph_llm_registry=resolved_graph_llm_registry,
        graph_llm_provider_name=DEFAULT_LLM_PROVIDER_NAME if build_graph else None,
        graph_terms=resolved_graph_terms,
        graph_client=resolved_graph_client,
        graph_review_conn=resolved_graph_review_conn,
        ocr=resolved_ocr,
        ocr_render_dpi=resolved_settings.ocr_render_dpi,
        ocr_max_concurrency=resolved_settings.ocr_max_concurrency,
        table_extractor=resolved_table_extractor,
        table_extraction_max_concurrency=resolved_settings.table_extraction_max_concurrency,
        job_concurrency=resolved_settings.ingestion_job_concurrency,
        limit=limit,
    )
```

- [ ] **Step 8: 运行测试确认通过**

Run: `python -m pytest tests/ingestion/test_ingestion_queue.py tests/api/test_admin_document_routes.py tests/ingestion/test_incremental_main.py -v`
Expected: PASS（全部测试；`test_admin_document_routes.py`/`test_incremental_main.py` 目前没有专门断言 `ocr_max_concurrency` 这类参数透传的测试用例，`job_concurrency` 同理不需要新增，跑这两个文件只是确认新增的参数没有打破现有调用路径）

- [ ] **Step 9: 提交**

```bash
git add app/ingestion/ingestion_queue.py app/config/settings.py app/api/admin_document_routes.py app/ingestion/incremental_main.py tests/ingestion/test_ingestion_queue.py
git commit -m "feat(ingestion): add opt-in cross-document concurrency to process_pending_jobs, defaulting to serial"
```

---

## Final verification

- [ ] **运行全量测试套件**

Run: `python -m pytest tests/ -q`
Expected: 除 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`（会话开始前就存在、与本计划无关的失败）之外全部 PASS

- [ ] **重启后端服务，确认能正常启动**

Run（Windows / 本仓库约定的重启方式）：

```bash
# 找到监听 8000 端口的进程并停止，然后：
cd D:/project/customer_rag && nohup .venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > backend.log 2>&1 &
```

Expected: `backend.log` 里出现 `Application startup complete.`，`curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs` 返回 `200`

- [ ] **用真实问题走一遍 `/qa` 和 `/agent/chat`，确认回答内容没有变化**

用报告里同样的真实问题（"宁德时代员工中博士有多少人"，`tenant_id=demo`）分别打一次 `/qa` 和 `/agent/chat`，确认能正常拿到答案（不是 500/超时），内容语义上和改造前一致——本计划只改执行顺序，不改任何检索/生成逻辑，如果回答内容发生实质变化，说明某个并发化改动破坏了数据依赖，需要回头排查，不能直接认为"能跑通就是对的"。
