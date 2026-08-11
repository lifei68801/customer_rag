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
