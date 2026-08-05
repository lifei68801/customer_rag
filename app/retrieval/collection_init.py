from __future__ import annotations

from typing import Callable, Protocol

from app.config.settings import Settings


class CollectionAdminClient(Protocol):
    def has_collection(self, collection_name: str) -> bool: ...

    def create_collection(self, collection_name: str, **kwargs) -> None: ...


_ID_MAX_LENGTH = 512


def ensure_collection(
    client: CollectionAdminClient,
    *,
    collection_name: str,
    dimension: int,
) -> bool:
    """确保 collection 存在；不存在则创建，返回是否新建了 collection。

    id 用 string 类型（对应我们的 VectorRecord.id，通常是文档来源路径），
    并开启 dynamic field，让 text/metadata/tenant_id 这些字段无需预先声明 schema。

    租户隔离限制：tenant_id 目前是 dynamic field，MilvusVectorStore.search()
    用它做 filter 表达式是正确的，但没有专门的标量索引——租户/数据量大了之后
    查询会退化成全表扫描再过滤。真要扩规模，需要改用完整 CollectionSchema，
    把 tenant_id 声明成带索引的标量字段。
    """
    if client.has_collection(collection_name):
        return False
    client.create_collection(
        collection_name,
        dimension=dimension,
        id_type="string",
        max_length=_ID_MAX_LENGTH,
        enable_dynamic_field=True,
    )
    return True


def _default_client_factory(uri: str) -> CollectionAdminClient:
    from pymilvus import MilvusClient

    return MilvusClient(uri=uri)


def main(
    *,
    settings: Settings | None = None,
    client_factory: Callable[[str], CollectionAdminClient] | None = None,
) -> bool:
    """初始化脚本入口：确保 Milvus collection 就绪。

    用法：python -m app.retrieval.collection_init
    """
    resolved_settings = settings or Settings()
    factory = client_factory or _default_client_factory
    client = factory(resolved_settings.milvus_uri)
    created = ensure_collection(
        client,
        collection_name=resolved_settings.milvus_collection,
        dimension=resolved_settings.embedding_dimension,
    )
    if created:
        print(f"已创建 collection: {resolved_settings.milvus_collection}")
    else:
        print(f"collection 已存在，跳过: {resolved_settings.milvus_collection}")
    return created


if __name__ == "__main__":
    main()
