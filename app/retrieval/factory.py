from __future__ import annotations

from typing import Callable

from app.config.settings import Settings
from app.retrieval.milvus_store import MilvusClientProtocol, MilvusVectorStore


def _default_client_factory(uri: str) -> MilvusClientProtocol:
    from pymilvus import MilvusClient

    return MilvusClient(uri=uri)


def build_vector_store_from_settings(
    settings: Settings,
    *,
    client_factory: Callable[[str], MilvusClientProtocol] | None = None,
) -> MilvusVectorStore:
    factory = client_factory or _default_client_factory
    client = factory(settings.milvus.uri)
    return MilvusVectorStore(
        client=client, collection_name=settings.milvus.collection
    )
