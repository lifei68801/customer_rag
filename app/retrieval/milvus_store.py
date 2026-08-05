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

    def query(
        self,
        *,
        collection_name: str,
        filter: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]: ...


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

    async def list_all(self, *, limit: int = 10000) -> list[VectorRecord]:
        """取出 collection 内全部记录，供 BM25 等需要全量语料的模块重建索引使用。

        主键是 VARCHAR（见 collection_init.py），用 `id != ""` 作为
        "匹配全部"过滤条件——所有写入的 id 均非空，等价于无条件查询。
        """
        rows = await asyncio.to_thread(
            self._client.query,
            collection_name=self._collection_name,
            filter='id != ""',
            limit=limit,
        )
        return [
            VectorRecord(
                id=str(row["id"]),
                vector=[],
                text=str(row.get("text", "")),
                metadata={
                    k: v for k, v in row.items() if k not in {"id", "text"}
                },
            )
            for row in rows
        ]
