# BM25 预建倒排索引 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `app/retrieval/bm25.py::BM25Index` 从"每次 `search()` 都全量扫描该租户全部文档重算 tf/idf"重构成按租户维护的真正倒排索引（token → 文档），`search()` 只处理真正包含至少一个查询词的候选文档，且和旧实现产出数学上完全等价的排序结果。

**Architecture:** 单一类内部重构，对外 API（`index(records)` / `search(query, *, top_k, tenant_id)`）不变。核心变化：把"一个全局扁平列表 + 每次按 tenant_id 过滤"换成 `dict[tenant_id, _TenantIndex]`，每个 `_TenantIndex` 维护自己的 `postings`（token→文档本地下标集合）、增量更新的 `doc_freq`/`total_token_count`。详见设计文档。

**Tech Stack:** Python 3.12 / pytest，不引入任何新依赖。

## Global Constraints

- **正确性不变量（本计划最核心的约束）**：新实现和旧实现（全量扫描版）对同一批数据、同一个查询，必须给出完全一致的排序结果和分数（不是"差不多"，是逐字段相等）——候选集筛选在数学上是精确等价的优化，不是近似。
- 对外 API 不变：`BM25Index()`、`.index(records)`、`.search(query, *, top_k, tenant_id) -> list[BM25Hit]` 的签名和返回类型不变。
- `tests/retrieval/test_bm25.py` 里的 3 个既有测试必须原样通过，一个字都不改。
- `_tokenize`/`_TOKEN_PATTERN`（中文按单字切分）的分词逻辑不变，只改索引和查询用到的数据结构。
- 中文注释只写"为什么"，不写"是什么"；不加多余的错误处理/校验；不引入第三方 BM25 库。
- 参见设计文档：`docs/superpowers/specs/2026-08-11-bm25-inverted-index-design.md`。

---

## Task 1: 用倒排索引重写 `BM25Index`

**Files:**
- Modify: `app/retrieval/bm25.py`（整个文件重写 `BM25Index` 类，`BM25Hit`/`_tokenize`/`_TOKEN_PATTERN`/`build_bm25_index_from_store` 不变）
- Modify: `tests/retrieval/test_bm25.py`（新增测试，既有 3 个测试保持原样）

**Interfaces:**
- Consumes: 无新依赖，`app.retrieval.vector_store.VectorRecord`（不变）
- Produces: `BM25Index`/`BM25Hit` 对外签名和返回值不变，供 `app/retrieval/hybrid_search.py`、`app/memory/recall.py`、`app/memory/structured_recall.py` 现有调用方直接复用，不需要改任何调用方代码

- [ ] **Step 1: 写一个针对新内部结构的失败测试（RED 锚点）**

当前实现没有 `_tenants`/`postings`/`doc_freq` 这些内部结构，下面这个测试引用它们，在实现之前必然因为 `AttributeError`（`BM25Index` 没有 `_tenants` 属性）失败——这是本任务的 TDD 锚点。差分对比测试（Step 6）在实现前后都会通过（因为对比的是"重构前后行为是否一致"，不是"新功能是否存在"），不能拿来当 RED 步骤用。

```python
# tests/retrieval/test_bm25.py 末尾追加
def test_index_incrementally_maintains_postings_and_doc_freq_per_tenant():
    """直接断言内部倒排表状态，不只测最终 search() 结果——方便日后如果
    排序结果算错了，能分清是"索引维护错了"还是"打分逻辑错了"。"""
    index = BM25Index()
    index.index(
        [VectorRecord(id="a", vector=[], text="网络故障", tenant_id="t1", metadata={})]
    )

    tenant = index._tenants["t1"]
    assert tenant.doc_freq["网"] == 1
    assert tenant.doc_freq["故"] == 1
    assert tenant.postings["网"] == {0}
    assert tenant.total_token_count == 4  # "网" "络" "故" "障" 四个单字 token

    # 增量追加第二条记录，验证是合并进已有租户索引，不是覆盖或重算
    index.index(
        [VectorRecord(id="b", vector=[], text="网络连接超时", tenant_id="t1", metadata={})]
    )

    tenant = index._tenants["t1"]
    assert tenant.doc_freq["网"] == 2  # 两条记录都含"网"
    assert tenant.postings["网"] == {0, 1}
    assert tenant.total_token_count == 4 + 6  # 第二条记录 6 个单字 token

    # 另一个租户的索引完全独立，不受 t1 的更新影响
    index.index(
        [VectorRecord(id="c", vector=[], text="网络故障", tenant_id="t2", metadata={})]
    )
    assert "t2" in index._tenants
    assert index._tenants["t1"].doc_freq["网"] == 2  # t1 的计数不受 t2 影响
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/retrieval/test_bm25.py::test_index_incrementally_maintains_postings_and_doc_freq_per_tenant -v`
Expected: FAIL，`AttributeError: 'BM25Index' object has no attribute '_tenants'`

- [ ] **Step 3: 重写 `app/retrieval/bm25.py`**

把整个文件替换为：

```python
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.retrieval.vector_store import VectorRecord, VectorStore

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


@dataclass(frozen=True)
class BM25Hit:
    id: str
    text: str
    score: float


@dataclass
class _TenantIndex:
    """单个租户的倒排索引状态，`BM25Index` 按 tenant_id 各维护一份，
    完全独立——查询时直接定位到对应租户，不会扫描或过滤到其它租户的
    数据，也不会被其它租户的高频词污染候选集。
    """

    records: list[VectorRecord] = field(default_factory=list)
    tokenized_docs: list[list[str]] = field(default_factory=list)
    # token -> 包含它的文档本地下标集合，这就是倒排表本体：以前想知道
    # "哪些文档包含某个词"要把每篇文档翻一遍，现在直接查表。
    postings: dict[str, set[int]] = field(default_factory=dict)
    # token -> 包含它的文档数，随 index() 调用增量维护，不用每次 search()
    # 都重新扫描全部文档统计一遍。
    doc_freq: dict[str, int] = field(default_factory=dict)
    total_token_count: int = 0


class BM25Index:
    """BM25Okapi 关键词检索索引，中文按字符切分，按租户维护倒排索引。

    search() 用倒排表（postings）把候选集收窄到"真正包含至少一个查询词
    的文档"，不是每次都扫描该租户的全部文档——候选集筛选在数学上和全量
    扫描精确等价（不包含任何查询词的文档，BM25 公式下贡献分数恒为 0），
    不是用速度换精度的近似优化。见 docs/superpowers/specs/2026-08-11-
    bm25-inverted-index-design.md。
    """

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._tenants: dict[str, _TenantIndex] = {}

    def index(self, records: list[VectorRecord]) -> None:
        for record in records:
            tenant = self._tenants.setdefault(record.tenant_id, _TenantIndex())
            tokens = _tokenize(record.text)
            local_idx = len(tenant.records)
            tenant.records.append(record)
            tenant.tokenized_docs.append(tokens)
            tenant.total_token_count += len(tokens)
            for token in set(tokens):
                tenant.postings.setdefault(token, set()).add(local_idx)
                tenant.doc_freq[token] = tenant.doc_freq.get(token, 0) + 1

    def search(self, query: str, *, top_k: int, tenant_id: str) -> list[BM25Hit]:
        tenant = self._tenants.get(tenant_id)
        query_tokens = _tokenize(query)
        if not query_tokens or tenant is None:
            return []

        candidate_indices: set[int] = set()
        for token in query_tokens:
            candidate_indices |= tenant.postings.get(token, set())
        if not candidate_indices:
            return []

        doc_count = len(tenant.records)
        avgdl = tenant.total_token_count / doc_count

        idf: dict[str, float] = {}
        for token in set(query_tokens):
            freq = tenant.doc_freq.get(token, 0)
            idf[token] = math.log(1.0 + (doc_count - freq + 0.5) / (freq + 0.5))

        scored: list[tuple[float, VectorRecord]] = []
        # sorted() 而不是直接遍历 candidate_indices（一个 set，迭代顺序不
        # 保证稳定）——保证按原始文档插入顺序处理，分数打平时的 tie-break
        # 顺序才能和旧的全量扫描实现（按 scoped 列表原始顺序遍历）完全
        # 一致，不然差分测试在遇到同分文档时会因为顺序不同而失败，那不是
        # 排序逻辑错了，是这里偷懒用了不确定顺序的遍历。
        for local_idx in sorted(candidate_indices):
            tokens = tenant.tokenized_docs[local_idx]
            record = tenant.records[local_idx]
            term_freq: dict[str, int] = {}
            for token in tokens:
                term_freq[token] = term_freq.get(token, 0) + 1
            dl = len(tokens)
            score = 0.0
            for token in query_tokens:
                tf = term_freq.get(token, 0)
                if tf <= 0:
                    continue
                denom = tf + self._k1 * (
                    1.0 - self._b + self._b * (dl / avgdl)
                )
                score += idf[token] * ((tf * (self._k1 + 1.0)) / denom)
            if score > 0:
                scored.append((score, record))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            BM25Hit(id=record.id, text=record.text, score=score)
            for score, record in scored[:top_k]
        ]


async def build_bm25_index_from_store(store: VectorStore) -> BM25Index:
    """从向量库全量拉取记录重建 BM25 索引。

    BM25Index 是进程内内存结构，摄取脚本和 API 服务是两个独立进程，
    摄取时建的索引对 API 进程不可见；因此改为 API 进程启动/首次访问时
    从向量库（唯一的事实来源）重建，而不是尝试跨进程共享索引状态。
    """
    index = BM25Index()
    records = await store.list_all()
    index.index(records)
    return index
```

- [ ] **Step 4: 运行 Step 1 的测试确认通过**

Run: `python -m pytest tests/retrieval/test_bm25.py::test_index_incrementally_maintains_postings_and_doc_freq_per_tenant -v`
Expected: PASS

- [ ] **Step 5: 运行既有 3 个测试确认原样通过（行为不变性的第一道验证）**

Run: `python -m pytest tests/retrieval/test_bm25.py::test_search_ranks_exact_keyword_match_above_unrelated_text tests/retrieval/test_bm25.py::test_search_returns_empty_when_no_terms_match tests/retrieval/test_bm25.py::test_search_does_not_return_hits_from_a_different_tenant -v`
Expected: PASS（这 3 个测试的源代码一个字都没改，纯粹验证新实现兑现了旧实现的外部行为）

- [ ] **Step 6: 写差分测试——新实现必须和旧的全量扫描算法逐字段完全等价**

这是本任务最重要的正确性证据：把旧算法原样复制成一个独立的参照实现（不依赖 `BM25Index` 的任何内部结构），拿同一批数据、同一组查询分别喂给新实现和参照实现，断言结果逐字段相等。

```python
# tests/retrieval/test_bm25.py 顶部 import 区加：
import math

# 文件末尾追加：
def _reference_full_scan_bm25_search(
    records: list[VectorRecord],
    query: str,
    *,
    top_k: int,
    tenant_id: str,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[str, float]]:
    """重构前 BM25Index.search() 的算法原样复制，作为独立参照实现——
    不调用被测的 BM25Index，只用来和新的倒排索引实现做差分对比，证明
    这次重构没有偷偷改变排序/打分语义。"""
    query_tokens = _tokenize(query)
    scoped = [
        (record, _tokenize(record.text))
        for record in records
        if record.tenant_id == tenant_id
    ]
    if not query_tokens or not scoped:
        return []

    scoped_docs = [tokens for _, tokens in scoped]
    doc_count = len(scoped_docs)
    avgdl = sum(len(d) for d in scoped_docs) / doc_count

    doc_freq: dict[str, int] = {}
    for tokens in scoped_docs:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    idf: dict[str, float] = {}
    for token in set(query_tokens):
        freq = doc_freq.get(token, 0)
        idf[token] = math.log(1.0 + (doc_count - freq + 0.5) / (freq + 0.5))

    scored: list[tuple[float, VectorRecord]] = []
    for record, tokens in scoped:
        if not tokens:
            continue
        term_freq: dict[str, int] = {}
        for token in tokens:
            term_freq[token] = term_freq.get(token, 0) + 1
        dl = len(tokens)
        score = 0.0
        for token in query_tokens:
            tf = term_freq.get(token, 0)
            if tf <= 0:
                continue
            denom = tf + k1 * (1.0 - b + b * (dl / avgdl))
            score += idf[token] * ((tf * (k1 + 1.0)) / denom)
        if score > 0:
            scored.append((score, record))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [(record.id, score) for score, record in scored[:top_k]]


_DIFFERENTIAL_TEST_RECORDS = [
    VectorRecord(id="t1-1", vector=[], text="网络故障排查手册第一章", tenant_id="t1", metadata={}),
    VectorRecord(id="t1-2", vector=[], text="登录失败排查手册", tenant_id="t1", metadata={}),
    VectorRecord(id="t1-3", vector=[], text="错误码E502网关超时排查", tenant_id="t1", metadata={}),
    VectorRecord(id="t1-4", vector=[], text="账号密码重置流程说明", tenant_id="t1", metadata={}),
    VectorRecord(id="t2-1", vector=[], text="网络故障排查手册第一章", tenant_id="t2", metadata={}),
    VectorRecord(id="t2-2", vector=[], text="发票开具流程说明文档", tenant_id="t2", metadata={}),
]

_DIFFERENTIAL_TEST_QUERIES = [
    ("网络故障怎么排查", "t1"),   # "排查" 在 t1 内是高频词（3 篇都有），弱区分度
    ("网络故障怎么排查", "t2"),   # 同一个查询在另一个租户，验证租户隔离下分数也独立
    ("错误码E502网关超时", "t1"),  # 混合中英文+数字的查询
    ("完全不存在的查询内容", "t1"),  # 查询词不在任何文档里，两边都应该返回空
    ("流程说明", "t1"),          # "流程说明" 在 t1/t2 各只有一篇命中，测试低频词
    ("流程说明", "t2"),
]


def test_new_implementation_matches_reference_full_scan_algorithm_exactly():
    index = BM25Index()
    index.index(_DIFFERENTIAL_TEST_RECORDS)

    for query, tenant_id in _DIFFERENTIAL_TEST_QUERIES:
        new_hits = index.search(query, top_k=10, tenant_id=tenant_id)
        new_result = [(hit.id, hit.score) for hit in new_hits]
        reference_result = _reference_full_scan_bm25_search(
            _DIFFERENTIAL_TEST_RECORDS, query, top_k=10, tenant_id=tenant_id
        )
        assert new_result == reference_result, (
            f"query={query!r} tenant_id={tenant_id!r}: "
            f"新实现={new_result} 参照实现={reference_result}"
        )
```

- [ ] **Step 7: 运行差分测试确认通过**

Run: `python -m pytest tests/retrieval/test_bm25.py::test_new_implementation_matches_reference_full_scan_algorithm_exactly -v`
Expected: PASS。如果失败，先检查是不是 `search()` 里用了 `candidate_indices`（一个 set）直接遍历而不是 `sorted(candidate_indices)`——同分文档的相对顺序会因为 set 迭代顺序不确定而和参照实现的顺序对不上，这是最容易踩的坑，Step 3 的代码已经用了 `sorted()`，如果这里失败先确认没有手滑漏掉。

- [ ] **Step 8: 补边界情况测试**

```python
# tests/retrieval/test_bm25.py 末尾追加
def test_search_on_empty_index_returns_empty():
    index = BM25Index()
    assert index.search("任意查询", top_k=5, tenant_id="t1") == []


def test_search_with_query_that_matches_no_document_returns_empty_without_scanning_error():
    index = BM25Index()
    index.index(
        [VectorRecord(id="a", vector=[], text="网络故障", tenant_id="t1", metadata={})]
    )
    # 查询词完全不在任何文档的 postings 里，candidate_indices 应该是空集，
    # 提前返回而不是继续往下跑除零之类的路径。
    assert index.search("完全无关内容", top_k=5, tenant_id="t1") == []


def test_search_with_single_document_corpus_does_not_divide_by_zero():
    index = BM25Index()
    index.index(
        [VectorRecord(id="a", vector=[], text="网络故障", tenant_id="t1", metadata={})]
    )
    hits = index.search("网络", top_k=5, tenant_id="t1")
    assert len(hits) == 1
    assert hits[0].id == "a"
```

- [ ] **Step 9: 运行整个测试文件 + 全量测试套件确认通过**

Run: `python -m pytest tests/retrieval/test_bm25.py -v`
Expected: PASS（全部测试，既有 3 个 + 新增的 6 个）

Run: `python -m pytest tests/ -q`
Expected: 除 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured`（会话开始前就存在、与本计划无关的失败）之外全部 PASS

- [ ] **Step 10: 用真实数据做一次性能对比（记录证据，不写进 pytest 套件）**

这一步不是自动化测试的一部分——是为了留一份"这次重构真的有性能收益"的真实证据，用独立脚本跑，不要加进 `tests/` 目录：

```python
# 独立脚本，跑完即可丢弃，不要提交进仓库
import asyncio
import time

from app.config.settings import Settings
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.bm25 import build_bm25_index_from_store


async def main():
    settings = Settings()
    store = build_vector_store_from_settings(settings)
    index = await build_bm25_index_from_store(store)

    query = "宁德时代员工中博士有多少人"
    tenant_id = "demo"

    durations = []
    for _ in range(5):
        t0 = time.perf_counter()
        index.search(query, top_k=10, tenant_id=tenant_id)
        durations.append(time.perf_counter() - t0)

    print(f"n_records_in_tenant={sum(1 for r in index._tenants[tenant_id].records)}")
    print("search durations:", [f"{d*1000:.2f}ms" for d in durations])


asyncio.run(main())
```

Run 这个脚本，记录耗时。之前用旧实现对 `demo` 租户（5279 条记录）实测过单次 `search()` 是 3ms–308ms（见 `docs/superpowers/plans/2026-08-10-qa-and-ingestion-concurrency-optimization.md`）——这次用新实现跑同样的真实查询、同样的真实数据，把耗时记录进任务报告里，作为这次重构确实有收益的真实证据，不是理论推断。

- [ ] **Step 11: 提交**

```bash
git add app/retrieval/bm25.py tests/retrieval/test_bm25.py
git commit -m "perf(retrieval): rebuild BM25Index around a per-tenant inverted index instead of full-corpus rescans"
```

---

## Final verification

- [ ] **运行全量测试套件**

Run: `python -m pytest tests/ -q`
Expected: 除 `tests/providers/test_voice_factory.py::test_returns_none_when_tts_not_configured` 之外全部 PASS

- [ ] **重启后端服务，用真实问题走一遍 `/qa`，确认检索结果和回答内容没有变化**

`BM25Index` 是 `deps.get_bm25_index()` 里的进程内单例，重启后端才会重建。用之前验证过的真实问题（"宁德时代员工中博士有多少人"，`tenant_id=demo`）走一遍 `/qa` 或 `/agent/chat`，确认返回内容和这次重构之前一致（语义相同即可，不要求逐字相同——LLM 生成本身有随机性，但引用的 `used_sources`/检索到的资料应该是同一批）。
