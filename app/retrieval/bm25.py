from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.retrieval.vector_store import VectorRecord, VectorStore

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


@dataclass(frozen=True)
class BM25Hit:
    id: str
    text: str
    score: float


class BM25Index:
    """轻量级 BM25Okapi 关键词检索索引，中文按字符切分。"""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._records: list[VectorRecord] = []
        self._tokenized_docs: list[list[str]] = []

    def index(self, records: list[VectorRecord]) -> None:
        self._records.extend(records)
        self._tokenized_docs.extend(_tokenize(r.text) for r in records)

    def search(self, query: str, *, top_k: int) -> list[BM25Hit]:
        query_tokens = _tokenize(query)
        if not query_tokens or not self._tokenized_docs:
            return []

        doc_count = len(self._tokenized_docs)
        avgdl = sum(len(d) for d in self._tokenized_docs) / doc_count

        doc_freq: dict[str, int] = {}
        for tokens in self._tokenized_docs:
            for token in set(tokens):
                doc_freq[token] = doc_freq.get(token, 0) + 1

        idf: dict[str, float] = {}
        for token in set(query_tokens):
            freq = doc_freq.get(token, 0)
            idf[token] = math.log(1.0 + (doc_count - freq + 0.5) / (freq + 0.5))

        scored: list[tuple[float, VectorRecord]] = []
        for record, tokens in zip(self._records, self._tokenized_docs):
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
