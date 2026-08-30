from __future__ import annotations

import dataclasses
from app.retrieval.vector_math import cosine_similarity
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class VectorRecord:
    id: str
    vector: list[float]
    text: str
    tenant_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # search() 返回的结果会填充（余弦相似度/Milvus距离，越大越相关）；
    # hybrid_search() 配置了 rerank_provider 时还会在融合后被覆盖成 rerank
    # 的 relevance_score（不同量纲，见 app/retrieval/hybrid_search.py）。
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

    async def list_by_source(self, *, source: str, tenant_id: str) -> list[VectorRecord]: ...

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None: ...



def chunk_index_from_id(record_id: str) -> int:
    """从 f"{path}#{i}" 形式的 id 里取出序号 i（见
    app/ingestion/pipeline.py::_embed_and_upsert 写入时的编号方式）。

    向量库本身（Milvus 的 query()、InMemoryVectorStore 内部的 list）都不
    保证任何返回顺序，list_by_source() 靠这个函数重新按文档原始 chunk
    顺序排序，预览页面才能按写入时的先后展示，而不是一堆乱序的片段。
    id 不含 "#" 或后缀不是数字（理论上不会发生，写入路径固定用这个格式，
    这里只是防御性兜底）时返回 0，不抛异常中断整个排序。
    """
    _, _, suffix = record_id.rpartition("#")
    try:
        return int(suffix)
    except ValueError:
        return 0


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
            (cosine_similarity(query_vector, record.vector), record)
            for record in self._records
            if record.tenant_id == tenant_id
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            dataclasses.replace(record, score=score) for score, record in scored[:top_k]
        ]

    async def list_all(self) -> list[VectorRecord]:
        return list(self._records)

    async def list_by_source(
        self, *, source: str, tenant_id: str
    ) -> list[VectorRecord]:
        matched = [
            r
            for r in self._records
            if r.tenant_id == tenant_id and r.metadata.get("source") == source
        ]
        matched.sort(key=lambda r: chunk_index_from_id(r.id))
        return matched

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
        self._records = [
            r
            for r in self._records
            if not (r.tenant_id == tenant_id and r.metadata.get("source") == source)
        ]
