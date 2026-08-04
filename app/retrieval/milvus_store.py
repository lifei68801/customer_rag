from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.retrieval.vector_store import VectorRecord


class MilvusClientProtocol(Protocol):
    def insert(self, *, collection_name: str, data: list[dict[str, Any]]) -> Any: ...

    def search(
        self,
        *,
        collection_name: str,
        data: list[list[float]],
        limit: int,
        output_fields: list[str],
    ) -> Any: ...


class MilvusVectorStore:
    """真实 Milvus 后端的 VectorStore 实现。

    pymilvus 的 MilvusClient 是同步阻塞调用，用 asyncio.to_thread
    包一层以匹配 VectorStore 协议的 async 接口，不阻塞事件循环。
    """

    def __init__(
        self,
        *,
        client: MilvusClientProtocol,
        collection_name: str,
    ) -> None:
        self._client = client
        self._collection_name = collection_name

    async def upsert(self, records: list[VectorRecord]) -> None:
        data = [
            {
                "id": record.id,
                "vector": record.vector,
                "text": record.text,
                **record.metadata,
            }
            for record in records
        ]
        await asyncio.to_thread(
            self._client.insert,
            collection_name=self._collection_name,
            data=data,
        )

    async def search(
        self, query_vector: list[float], *, top_k: int
    ) -> list[VectorRecord]:
        results = await asyncio.to_thread(
            self._client.search,
            collection_name=self._collection_name,
            data=[query_vector],
            limit=top_k,
            output_fields=["text"],
        )
        hits = results[0] if results else []
        return [
            VectorRecord(
                id=str(hit["id"]),
                vector=[],
                text=hit["entity"]["text"],
                metadata={},
            )
            for hit in hits
        ]
