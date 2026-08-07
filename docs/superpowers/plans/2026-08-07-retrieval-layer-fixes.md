# 检索层修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 `app/retrieval/hybrid_search.py` 里两处与设计文档不一致的行为：rerank 精排分数没有回写覆盖到返回记录上（Fallback 转人工的判断实际没用到精排结果）；三路检索（原始query向量+改写query向量+BM25）是顺序执行不是并行。

**Architecture:** 两处改动都局限在 `hybrid_search` 这一个函数内：rerank 分支返回值改用 `dataclasses.replace(record, score=hit.relevance_score)` 生成新记录；`query_texts` 循环里的 `embed→search` 链路改用 `asyncio.gather` 并发执行，BM25 检索保持同步不纳入并发调度。

**Tech Stack:** Python 3.12、`asyncio.gather`、pytest（`asyncio_mode = "auto"`，测试函数直接写 `async def test_...`）。

## Global Constraints

- 严格 TDD：RED（写失败测试，确认失败原因正确）→ GREEN（最小实现）→ 跑全量测试 → git commit。
- 这是拆分出的 4 个独立子项目里的第 1 个（后续还有 TermGuard 模糊匹配、输入/输出安全增强、GraphRAG 实体链接模糊匹配 3 个独立子项目，各自单独走完整流程，不在本计划范围内）。
- `agent_min_relevance_score` 阈值语义从"向量余弦相似度"变为"rerank 精排分数"，已与用户确认可接受（当前代码库没有任何地方真正配置这个值，无实际部署影响），本计划需要在 `settings.py` 字段注释里补一句说明。
- 不新增独立的 `rerank_score` 字段，直接改变 `VectorRecord.score` 的语义（已确认）。
- 不改动 BM25 检索本身的并发调度，也不新增"验证真的并发执行"的时序类测试（价值有限、容易 flaky）——并行化以"融合结果不回归"作为验证标准。
- 不处理 HyDE（架构文档标注为可选进阶项，不在本次范围）。
- Commit message 格式：一行摘要（`feat:`/`fix:` 前缀）+ 空行 + 中文详细说明（为什么这么做/复用了什么/刻意不做什么）+ 以 `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` 结尾。
- 本仓库当前在 `dev/0.1` 分支直接工作，不建 worktree。
- 测试命令统一用 `.venv/Scripts/python.exe -m pytest <path> -v`（Windows 环境，本仓库自带 `.venv`）。
- 设计依据：`docs/superpowers/specs/2026-08-07-retrieval-layer-fixes-design.md`（已经用户批准，不要偏离其中的机制决策）。

---

### Task 1: rerank 分数回写

**Files:**
- Modify: `app/retrieval/hybrid_search.py`（顶部 import 区 + 函数末尾 return 语句）
- Modify: `app/config/settings.py:70-74`（`agent_min_relevance_score` 字段注释）
- Test: `tests/retrieval/test_hybrid_search.py`

**Interfaces:**
- Consumes：`app.providers.rerank.RerankHit`（已有，`index: int`/`relevance_score: float`）、`app.retrieval.vector_store.VectorRecord`（已有，frozen dataclass，`score: float | None = None`）。
- Produces：`hybrid_search(...)` 走 rerank 分支时，返回的每条 `VectorRecord.score` 等于对应 `RerankHit.relevance_score`，不再是融合前的向量检索原始相似度。这个行为是 Task 2（如果后续需要，虽然本计划只有这一个子项目）以及下游 `app/agent/graph.py::route_after_retrieval` 的既有消费者依赖的最终状态。

- [ ] **Step 1: 写失败测试**

在 `tests/retrieval/test_hybrid_search.py` 末尾追加（复用文件里已有的 `_build_store_and_index`/`FixedEmbeddingProvider`，以及 `test_hybrid_search_uses_rerank_order_when_provided` 用的 `FakeRerankProvider` 写法）：

```python
async def test_hybrid_search_rewrites_score_with_rerank_relevance():
    store, bm25 = await _build_store_and_index()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        "fake-embedding",
        FixedEmbeddingProvider({"E502 错误码是什么意思": [1.0, 0.0]}),
    )
    llm_registry = ProviderRegistry()

    class FakeRerankProvider:
        async def rerank(self, request: RerankRequest) -> RerankResult:
            # relevance_score 和融合排序位置无关，专门设成能一眼看出是不是被
            # 正确回写的值（0.42/0.17），验证下游拿到的是这两个数而不是向量
            # 检索阶段的原始相似度。
            hits = [
                RerankHit(index=i, relevance_score=0.42 if "网关超时" not in doc else 0.17)
                for i, doc in enumerate(request.documents)
            ]
            return RerankResult(hits=hits)

    results = await hybrid_search(
        "E502 错误码是什么意思",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=store,
        bm25_index=bm25,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        query_rewrite_enabled=False,
        rerank_provider=FakeRerankProvider(),
        final_top_k=2,
        tenant_id="t1",
    )

    scores_by_id = {record.id: record.score for record in results}
    assert scores_by_id["vector_hit"] == 0.42
    assert scores_by_id["bm25_hit"] == 0.17
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/retrieval/test_hybrid_search.py -v -k rewrites_score_with_rerank_relevance`
Expected: `AssertionError`——`scores_by_id["vector_hit"]` 实际是 `1.0`（向量检索阶段的原始相似度，因为 `FixedEmbeddingProvider` 把这条 query 映射到和 `vector_hit` 记录完全相同的向量 `[1.0, 0.0]`），不是期望的 `0.42`，说明当前代码确实没有回写 rerank 分数。

- [ ] **Step 3: 写最小实现**

`app/retrieval/hybrid_search.py` 顶部 import 区（当前第 1-9 行）追加一行：

```python
import dataclasses
```

放在 `from __future__ import annotations` 之后、其余 `from app...` 之前（保持 stdlib import 在前的常见习惯）：

```python
from __future__ import annotations

import dataclasses

from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider, RerankRequest
from app.qa.query_rewrite import rewrite_query
from app.retrieval.bm25 import BM25Index
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.vector_store import VectorRecord, VectorStore
```

把函数末尾的 return 语句（当前）：

```python
    rerank_result = await rerank_provider.rerank(
        RerankRequest(
            query=question,
            documents=[record.text for record in fused_records],
            top_n=final_top_k,
        )
    )
    return [fused_records[hit.index] for hit in rerank_result.hits[:final_top_k]]
```

替换为：

```python
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

同时把函数顶部的 docstring（当前第 31-38 行）里补一句说明这个行为：

```python
    """原始query向量检索 + 改写query向量检索 + BM25 三路 -> RRF融合 -> 可选Rerank。

    RRF 只依据各路排名位置融合，不比较向量相似度和 BM25 分数这两种不同量纲；
    Rerank 仅用于对融合后的候选池精排，不改变候选池本身，但会把每条记录的
    score 覆盖为 rerank 返回的 relevance_score——下游 Fallback 判断（见
    app/agent/graph.py::route_after_retrieval）依据的置信度分数因此来自
    精排结果而不是向量检索阶段的原始相似度。未配置 rerank_provider 时
    score 保持融合前的原始值不变。

    conversation_context 为可选项：传入近期对话轮次时，query 改写这一步
    能看到"用户之前说了什么"来补全模糊指代（"这个报错"），见
    app/qa/query_rewrite.py；不传则只看孤立的当前问题，行为不变。
    """
```

修改 `app/config/settings.py`，把 `agent_min_relevance_score` 字段的注释（当前第 70-73 行）：

```python
    # 真实向量库几乎总能返回 Top-K 个最近邻，哪怕语义上完全不相关；设置后，
    # 检索到的记录即使非空，最高相关性分数低于这个阈值也会转人工工单，而不
    # 是把不相关资料硬塞给 LLM。默认不设置（None），行为与之前完全一致——
    # 具体阈值需要结合实际 embedding 模型/语料标定，不能瞎猜一个通用值。
```

替换为：

```python
    # 真实向量库几乎总能返回 Top-K 个最近邻，哪怕语义上完全不相关；设置后，
    # 检索到的记录即使非空，最高相关性分数低于这个阈值也会转人工工单，而不
    # 是把不相关资料硬塞给 LLM。默认不设置（None），行为与之前完全一致——
    # 具体阈值需要结合实际 embedding 模型/语料标定，不能瞎猜一个通用值。
    # 注意：配置了 rerank_provider 时，这里比较的是 rerank 返回的
    # relevance_score（不同供应商的分数范围/语义可能不同，比如有的是 0-1
    # 概率值，有的是无界的原始 logit），不是向量检索阶段的余弦相似度
    # （0-1 有界）——标定这个阈值必须参照实际接入的 rerank 模型的分数
    # 分布，不能沿用向量相似度的经验值。未配置 rerank_provider 时才是
    # 比较向量相似度。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/retrieval/test_hybrid_search.py -v`
Expected: 5 passed（原有 4 个 + 新增 1 个）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过（这一步只改变了 rerank 分支的返回值语义，`route_after_retrieval` 目前没有任何测试依赖旧的"向量相似度"语义，不应该引入回归；如果发现有测试因为这个改动失败，说明存在本计划未预见到的依赖，需要先排查再继续，不要跳过）

- [ ] **Step 6: 提交**

```bash
git add app/retrieval/hybrid_search.py app/config/settings.py tests/retrieval/test_hybrid_search.py
git commit -m "fix: rewrite VectorRecord.score with rerank relevance_score"
```

---

### Task 2: 三路检索并行化

**Files:**
- Modify: `app/retrieval/hybrid_search.py`（顶部 import 区 + `query_texts` 循环部分）
- Test: `tests/retrieval/test_hybrid_search.py`

**Interfaces:**
- Consumes：无新依赖，`embedding_registry.run`/`vector_store.search` 签名不变（均为已有的 async 方法）。
- Produces：`hybrid_search` 对外行为不变（相同输入产生相同的候选集合与最终排序），只是内部执行方式从顺序 `await` 改为 `asyncio.gather` 并发——这一点通过"现有测试原样通过"来验证，不引入新的可观察接口。

- [ ] **Step 1: 写失败测试**

这个改动本身不产生新的可观察行为（融合结果集合不变），所以不是"先写一个新失败测试再实现"的典型 TDD 模式，而是"先确认现有测试覆盖了要保护的行为，再重构实现，跑测试确认没有回归"。跳过手写新测试这一步，直接进入 Step 2 用现有测试建立基线。

- [ ] **Step 2: 跑现有测试确认基线通过**

Run: `.venv/Scripts/python.exe -m pytest tests/retrieval/test_hybrid_search.py -v`
Expected: 5 passed（Task 1 结束时的状态——这是本任务开始前的基线，本任务的改动完成后这 5 个测试必须仍然全部通过，作为"没有引入回归"的验证依据）

- [ ] **Step 3: 写最小实现**

把 `app/retrieval/hybrid_search.py` 顶部 import 区（Task 1 结束后的状态）：

```python
from __future__ import annotations

import dataclasses

from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider, RerankRequest
from app.qa.query_rewrite import rewrite_query
from app.retrieval.bm25 import BM25Index
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.vector_store import VectorRecord, VectorStore
```

改为（新增 `asyncio` import）：

```python
from __future__ import annotations

import asyncio
import dataclasses

from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider, RerankRequest
from app.qa.query_rewrite import rewrite_query
from app.retrieval.bm25 import BM25Index
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.vector_store import VectorRecord, VectorStore
```

把函数体里 `query_texts` 循环部分（当前）：

```python
    for text in query_texts:
        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=[text]),
            provider_name=embedding_provider_name,
        )
        vector_hits = await vector_store.search(
            embed_result.vectors[0], top_k=vector_top_k, tenant_id=tenant_id
        )
        ranked_id_lists.append([record.id for record in vector_hits])
        for record in vector_hits:
            candidates[record.id] = record
```

替换为：

```python
    async def _vector_search_for_text(text: str) -> list[VectorRecord]:
        embed_result = await embedding_registry.run(
            EmbeddingRequest(texts=[text]),
            provider_name=embedding_provider_name,
        )
        return await vector_store.search(
            embed_result.vectors[0], top_k=vector_top_k, tenant_id=tenant_id
        )

    per_text_hits = await asyncio.gather(
        *(_vector_search_for_text(text) for text in query_texts)
    )
    for vector_hits in per_text_hits:
        ranked_id_lists.append([record.id for record in vector_hits])
        for record in vector_hits:
            candidates[record.id] = record
```

`_vector_search_for_text` 定义在 `query_texts` 已经确定之后（原始 query 是否需要改写的判断已经跑完），这样闭包捕获的 `embedding_registry`/`embedding_provider_name`/`vector_store`/`vector_top_k`/`tenant_id` 都是函数参数，不需要额外传参。`asyncio.gather` 保证返回顺序和输入顺序一致，`per_text_hits` 里第 i 个元素对应 `query_texts` 里第 i 个文本的检索结果，融合阶段的 `ranked_id_lists` 顺序因此和原来完全一致。

同时更新函数顶部的 docstring，在 Task 1 已经补充过的说明后面再加一句：

```python
    conversation_context 为可选项：传入近期对话轮次时，query 改写这一步
    能看到"用户之前说了什么"来补全模糊指代（"这个报错"），见
    app/qa/query_rewrite.py；不传则只看孤立的当前问题，行为不变。

    query_texts（原始问题 + 可能的改写问题）各自的向量检索用 asyncio.gather
    并发执行，而不是顺序等待——两条链路各自都是一次 embedding API 调用加一次
    Milvus 查询，顺序执行会让总延迟翻倍。BM25 检索是同步内存操作，没有 IO
    等待，不需要纳入并发调度。
    """
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/retrieval/test_hybrid_search.py -v`
Expected: 5 passed（和 Step 2 的基线数量一致，确认改动没有引入回归；特别关注 `test_hybrid_search_passes_conversation_context_to_query_rewrite` 这个测试——它验证的是 `rewrite_query` 这一步的行为，发生在 `query_texts` 循环之前，不受本任务改动影响，应该继续通过）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/retrieval/hybrid_search.py
git commit -m "feat: run per-query vector search branches concurrently"
```

---

## 完成后

两个任务全部提交后，`hybrid_search` 的 Fallback 置信度判断真正依据精排结果，且原始 query 与改写 query 的向量检索不再互相阻塞。架构覆盖度审计标记的检索层两项行为偏差解决。后续 3 个子项目（TermGuard 模糊匹配、输入/输出安全增强、GraphRAG 实体链接模糊匹配）按此前确认的顺序各自独立走完整流程，不在本计划范围内。
