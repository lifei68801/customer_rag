from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.retrieval.vector_store import VectorRecord, chunk_index_from_id
from app.tenancy import validate_tenant_id as _validate_tenant_id

# tenant_id 会被拼进 Milvus 过滤表达式字符串，不能参数化传递；白名单校验
# 字符集，防止过滤表达式注入（与 Neo4j 关系类型白名单同思路）。规则本身
# 放在 app/tenancy.py，供 HTTP 入口层复用同一份定义——见那里的说明。

# Milvus 服务端对单次 query() 的硬性限制：(offset+limit) 必须落在
# [1, 16384] 区间，超过直接报 MilvusException（不是静默截断），实测确认
# 见 MilvusVectorStore._query_all 的说明。这不是我们自己定的业务上限。
_MAX_QUERY_WINDOW = 16384


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
                score=hit["distance"],
            )
            for hit in hits
        ]

    async def delete_by_source(self, *, source: str, tenant_id: str) -> None:
        """删除某个来源文件（同一 tenant_id 下）写入过的全部记录——增量摄取
        重新处理一个已变更文件前，先清掉它旧版本产出的所有 chunk，避免
        新版本 chunk 数变少时残留旧 chunk（陈旧、且可能已经不准确的内容）
        永远留在向量库里污染检索结果。

        source 是我们自己摄取时写入的文件路径字符串，不是外部不可信输入，
        但仍然转义反斜杠和双引号防止意外break出过滤表达式的字符串字面量
        ——不用 tenant_id 那种白名单校验，因为任意合法文件路径本身就可能
        包含白名单之外的字符（空格、中文等）。反斜杠必须先转义（Windows
        路径分隔符本身就是反斜杠），否则一段形如 反斜杠+数字 的路径片段会被
        Milvus 的过滤表达式解析器当成非法转义序列，直接报
        "cannot parse expression" 而不是把它当纯文本——顺序不能反过来，
        否则会把双引号转义产生的反斜杠又转义一遍。
        """
        _validate_tenant_id(tenant_id)
        escaped_source = source.replace("\\", "\\\\").replace('"', '\\"')
        await asyncio.to_thread(
            self._client.delete,
            collection_name=self._collection_name,
            filter=f'tenant_id == "{tenant_id}" && source == "{escaped_source}"',
        )

    async def _query_all(self, *, filter: str) -> list[dict[str, Any]]:
        """按 filter 拉取全部匹配行，limit 用 _MAX_QUERY_WINDOW（16384）——
        这不是我们挑的数字，是 Milvus 服务端对单次 query() 的硬性
        "max query result window" 限制（超过会直接报错，不是静默截断，
        实测确认见下）。

        2026-08-27 的第一版实现改用过 MilvusClient.query_iterator（pymilvus
        官方的分页导出 API，理论上没有条数上限），但线上实测直接把服务
        打挂：query_iterator 内部翻页时会把上一批最后一条记录的主键值
        拼进游标过滤表达式（形如 `id > "<上一条id>"`），但没有对这个值
        做转义——这个项目的 id 是 `{文件路径}#{chunk序号}` 格式，Windows
        环境上传的路径必然含反斜杠，一旦某一批最后一条记录的 id 里有
        反斜杠，下一次翻页请求的过滤表达式直接解析失败，抛
        MilvusException 一路冒泡到 FastAPI 层变成 500（比这次要修的
        "10000 条硬截断"更糟——从"数据不全"变成"服务不可用"）。反斜杠/
        双引号转义是这个文件其它方法（delete_by_source/list_by_source
        自己构造 filter 时）一直有意识在做的事，但 query_iterator 走的是
        pymilvus 内部逻辑，我们没有入口去补这个转义。

        16384 这个上限意味着：collection 总量或单文档 chunk 数一旦超过
        它，这里仍然会截断——但至少不会崩溃，且这个数字有 Milvus 官方
        文档依据，不是凭感觉写的占位符。真正做到"不管多大都不截断"需要
        自己实现一套基于排序字段的安全游标分页（且要求 Milvus query 支持
        显式排序），这是一块本次没有覆盖到的已知缺口，留给之后专门评估。
        """
        rows = await asyncio.to_thread(
            self._client.query,
            collection_name=self._collection_name,
            filter=filter,
            limit=_MAX_QUERY_WINDOW,
        )
        return rows

    @staticmethod
    def _rows_to_records(rows: list[dict[str, Any]]) -> list[VectorRecord]:
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

    async def list_by_source(
        self, *, source: str, tenant_id: str
    ) -> list[VectorRecord]:
        """查某个来源文件（同一 tenant_id 下）写入过的全部 chunk，供管理后台
        预览"这份文档到底被切成了什么、能不能被检索到"用。过滤表达式和
        转义规则跟 delete_by_source() 完全一致（同一个 source+tenant_id
        过滤维度），只是这里是只读查询不是删除——具体转义原因见
        delete_by_source() 的说明，这里不重复。

        Milvus 不保证 query 返回顺序，这里按 chunk_index_from_id() 重新
        排成文档原始顺序，调用方（管理后台预览接口）不需要自己再排一遍。
        """
        _validate_tenant_id(tenant_id)
        escaped_source = source.replace("\\", "\\\\").replace('"', '\\"')
        rows = await self._query_all(
            filter=f'tenant_id == "{tenant_id}" && source == "{escaped_source}"',
        )
        records = self._rows_to_records(rows)
        records.sort(key=lambda r: chunk_index_from_id(r.id))
        return records

    async def list_all(self) -> list[VectorRecord]:
        """取出 collection 内全部记录（不区分租户），供 BM25 等需要全量语料的
        模块重建索引使用；租户隔离在 BM25Index.search() 查询时按 tenant_id 过滤。

        主键是 VARCHAR（见 collection_init.py），用 `id != ""` 作为
        "匹配全部"过滤条件——所有写入的 id 均非空，等价于无条件查询。
        """
        rows = await self._query_all(filter='id != ""')
        return self._rows_to_records(rows)
