from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol

from app.retrieval.vector_store import VectorRecord

_SAFE_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_tenant_id(tenant_id: str) -> None:
    """tenant_id 会被拼进 Milvus 过滤表达式字符串，不能参数化传递；
    白名单校验字符集，防止过滤表达式注入（与 Neo4j 关系类型白名单同思路）。
    """
    if not _SAFE_TENANT_ID_PATTERN.match(tenant_id):
        raise ValueError(f"非法 tenant_id: {tenant_id!r}")


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

    def delete(
        self,
        *,
        collection_name: str,
        filter: str,
        **kwargs: Any,
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
                "tenant_id": record.tenant_id,
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
        self, query_vector: list[float], *, top_k: int, tenant_id: str
    ) -> list[VectorRecord]:
        _validate_tenant_id(tenant_id)
        results = await asyncio.to_thread(
            self._client.search,
            collection_name=self._collection_name,
            data=[query_vector],
            limit=top_k,
            filter=f'tenant_id == "{tenant_id}"',
            output_fields=["text"],
        )
        hits = results[0] if results else []
        return [
            VectorRecord(
                id=str(hit["id"]),
                vector=[],
                text=hit["entity"]["text"],
                tenant_id=tenant_id,
                metadata={},
            )
            for hit in hits
        ]

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
        """删除某个来源文件（同一 tenant_id 下）写入过的全部记录——增量摄取
        重新处理一个已变更文件前，先清掉它旧版本产出的所有 chunk，避免
        新版本 chunk 数变少时残留旧 chunk（陈旧、且可能已经不准确的内容）
        永远留在向量库里污染检索结果。

        source 是我们自己摄取时写入的文件路径字符串，不是外部不可信输入，
        但仍然转义双引号防止意外break出过滤表达式的字符串字面量——不用
        tenant_id 那种白名单校验，因为任意合法文件路径本身就可能包含
        白名单之外的字符（空格、中文等）。
        """
        _validate_tenant_id(tenant_id)
        escaped_source = source.replace('"', '\\"')
        await asyncio.to_thread(
            self._client.delete,
            collection_name=self._collection_name,
            filter=f'tenant_id == "{tenant_id}" && source == "{escaped_source}"',
        )

    async def list_all(self, *, limit: int = 10000) -> list[VectorRecord]:
        """取出 collection 内全部记录（不区分租户），供 BM25 等需要全量语料的
        模块重建索引使用；租户隔离在 BM25Index.search() 查询时按 tenant_id 过滤。

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
                tenant_id=str(row.get("tenant_id", "")),
                metadata={
                    k: v
                    for k, v in row.items()
                    if k not in {"id", "text", "tenant_id"}
                },
            )
            for row in rows
        ]
