from __future__ import annotations

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
        return [record for _, record in scored[:top_k]]

    async def list_all(self) -> list[VectorRecord]:
        return list(self._records)

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
        self._records = [
            r
            for r in self._records
            if not (r.tenant_id == tenant_id and r.metadata.get("source") == source)
        ]
