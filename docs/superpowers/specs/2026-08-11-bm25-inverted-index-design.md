# BM25 预建倒排索引 Design

## 背景与问题

`app/retrieval/bm25.py::BM25Index` 目前没有预建倒排索引。`index()` 只是把记录和分词结果原样存进两个并行列表（`self._records` / `self._tokenized_docs`），没有任何 `token → 文档` 的反向映射。每次 `search()` 调用都要：

1. 遍历**全部租户**的全部记录做 tenant 过滤；
2. 对过滤后这个租户的全部文档重新计算一遍 `doc_freq`/`idf`；
3. 再遍历一遍该租户全部文档打分。

三步都是 O(该租户文档总数) 的线性扫描，且是每次查询从零重算，不是增量维护的索引结构。这是 2026-08-10《端到端流程时间最优性分析》报告和随后的并发优化计划（`docs/superpowers/plans/2026-08-10-qa-and-ingestion-concurrency-optimization.md` Task 1）里明确记录、但刻意排除的 P2 项——Task 1 只解决了"是否阻塞事件循环"（`asyncio.to_thread` + 和其它调用并发跑），没有改变计算本身的复杂度。

本设计解决的是复杂度本身：给 `BM25Index` 加上真正的倒排索引，把 `search()` 从"扫描该租户全部文档"降到"只处理真正包含至少一个查询词的候选文档"。

## 范围

只重构 `app/retrieval/bm25.py::BM25Index` 类本身，对外 API（`index(records)` / `search(query, *, top_k, tenant_id)`）保持不变。

`BM25Index` 还有两处生产调用方——`app/memory/recall.py`、`app/memory/structured_recall.py`——它们的用法是"每次调用现建一个临时 `BM25Index()`、对少量记忆条目建一次索引、用一次就丢"，和 `hybrid_search.py` 用的进程级长期维护主索引是完全不同的使用模式（数据量小、一次性），本次不涉及；API 不变意味着这两处调用方自然继续可用，只是不会从倒排索引这个优化里获得明显收益（本来数据量就小）。

## 架构

### 数据结构

按租户分开维护，每个租户一份独立的倒排索引：

```python
@dataclass
class _TenantIndex:
    records: list[VectorRecord]        # 该租户自己的文档，用本地下标（不是全局下标）
    tokenized_docs: list[list[str]]    # 与 records 一一对应
    postings: dict[str, set[int]]      # token -> 包含它的文档本地下标集合（倒排表本体）
    doc_freq: dict[str, int]           # token -> 包含它的文档数，增量维护
    total_token_count: int             # 用于算 avgdl（平均文档长度）


class BM25Index:
    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._tenants: dict[str, _TenantIndex] = {}
```

每个租户完全独立一份结构，查询时直接定位到对应租户的索引，不会扫描或过滤到其它租户的数据——既是隔离考量，也避免了"先全局扫描、再按 tenant_id 过滤"这种做法在多租户场景下候选列表可能被无关租户的热门词污染变长的问题。

### `index()` 增量更新

```python
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
```

每条新记录只做一次分词，把自己加进对应租户的倒排表，`doc_freq` 逐 token 累加，不是每次调用都重新扫描全部文档统计一遍。`index()` 允许被多次调用（和现在的语义一致），多次调用时新记录正确合并进已有的租户索引。生产环境和现有测试代码实际都只调用一次（`build_bm25_index_from_store` 一次性传入全量记录），保留"可多次追加"只是不破坏现有 API 约定，不是为了一个真实存在的高频增量更新场景做专门优化。

### `search()` 查询

```python
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

    idf = {
        token: math.log(
            1.0 + (doc_count - tenant.doc_freq.get(token, 0) + 0.5)
            / (tenant.doc_freq.get(token, 0) + 0.5)
        )
        for token in set(query_tokens)
    }

    scored: list[tuple[float, VectorRecord]] = []
    for local_idx in candidate_indices:
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
            denom = tf + self._k1 * (1.0 - self._b + self._b * (dl / avgdl))
            score += idf[token] * ((tf * (self._k1 + 1.0)) / denom)
        if score > 0:
            scored.append((score, record))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [BM25Hit(id=r.id, text=r.text, score=s) for s, r in scored[:top_k]]
```

打分公式本身一字不改。`idf` 依然用**整个租户**的 `doc_freq`/`doc_count`（不是候选集内部的统计量）——候选集筛选只是跳过"必然算出 0 分"的文档（不包含任何查询词的文档，BM25 公式下 tf 恒为 0，贡献分数恒为 0），不改变 idf 的计算口径，否则会算出错误的分数。这是一个数学上精确等价的优化，不是近似。

### 正确性不变量

**新实现和旧实现（全量扫描版）对同一批数据、同一个查询，必须给出完全一致的排序结果和分数**——不是"用速度换一点精度损失"，是精确等价。

## 测试策略

1. **迁移现有测试**：`tests/retrieval/test_bm25.py` 的 3 个既有测试（精确匹配排名靠前、无匹配返回空、租户隔离）原样通过，不改测试内容——最直接的行为不变性验证。
2. **差分测试（新增，最关键）**：构造一批有代表性的测试数据（高频词、低频词、多租户混合），同时跑"旧的全量扫描算法"（作为参照实现保留在测试文件里，不进生产代码）和"新的倒排索引算法"，断言两者对同一组查询给出的 `(id, score)` 列表逐字段完全相等。
3. **倒排表内部状态单测**：多次调用 `index()` 追加记录，直接断言 `postings`/`doc_freq`/`total_token_count` 增量更新的正确性，不只测最终 `search()` 结果——方便定位是索引维护错了还是打分逻辑错了。
4. **边界情况**：空查询、查询词都不在任何文档里（`candidate_indices` 为空提前返回 `[]`）、租户不存在、单文档语料。
5. **真实数据性能对比**（记录在案，不是断言）：用 demo 租户现有的真实数据（5000+ 条记录），新旧实现各跑一遍同样的查询，记录耗时差异，作为这次优化实际收益的真实证据。

## 不做的事

- 不改 `_tokenize`/`_TOKEN_PATTERN` 的分词逻辑（中文按单字切分），只改索引和查询的数据结构。
- 不引入第三方 BM25 库（评估过 `rank_bm25` 等，权衡见需求讨论：会引入新依赖，且需要重新验证检索行为语义是否变化，风险和改动量都比自建倒排索引大）。
- 不处理 `app/memory/recall.py`/`app/memory/structured_recall.py` 那两处一次性小规模用法的性能（不在本次范围内，API 不变的前提下它们自然兼容）。
- 不支持"删除已索引的文档"这个操作——当前 `BM25Index` 本来就没有 delete，进程重启后从向量库重建是既有设计（见 `build_bm25_index_from_store` 的说明），本次不改变这个模型。
