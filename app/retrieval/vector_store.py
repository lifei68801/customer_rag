from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class VectorRecord:
    id: str
    vector: list[float]
    text: str
    tenant_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # 只有 search() 返回的结果才会填充（余弦相似度/Milvus距离，越大越相关）；
    # upsert 时新建的记录没有意义，保持 None。用于 Agent 兜底路径判断"检索
    # 到的结果是不是真的相关"——真实向量库几乎总能返回 Top-K 个最近邻，
    # 不能只靠"结果是否为空"判断需不需要转人工。
    score: float | None = None


class VectorStore(Protocol):
    async def upsert(self, records: list[VectorRecord]) -> None: ...

    async def search(
        self, query_vector: list[float], *, top_k: int, tenant_id: str
    ) -> list[VectorRecord]: ...

    async def list_all(self) -> list[VectorRecord]: ...

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None: ...


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class InMemoryVectorStore:
    """基于余弦相似度的内存实现。

    用于测试和本地跑通 MVP 检索链路，不是 Milvus 的生产替代品；
    Milvus 版实现是独立的 adapter，接口保持一致即可互换。
    """

    def __init__(self) -> None:
        self._records: list[VectorRecord] = []

    async def upsert(self, records: list[VectorRecord]) -> None:
        self._records.extend(records)

    async def search(
        self, query_vector: list[float], *, top_k: int, tenant_id: str
    ) -> list[VectorRecord]:
        scored = [
            (_cosine_similarity(query_vector, record.vector), record)
            for record in self._records
            if record.tenant_id == tenant_id
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            dataclasses.replace(record, score=score) for score, record in scored[:top_k]
        ]

    async def list_all(self) -> list[VectorRecord]:
        return list(self._records)

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
        self._records = [
            r
            for r in self._records
            if not (r.tenant_id == tenant_id and r.metadata.get("source") == source)
        ]
