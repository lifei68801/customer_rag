from __future__ import annotations

import asyncio
from functools import lru_cache

from fastapi import Depends

from app.config.settings import Settings
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    DEFAULT_LLM_PROVIDER_NAME,
    build_embedding_registry_from_settings,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.providers.rerank_factory import build_rerank_provider_from_settings
from app.retrieval.bm25 import BM25Index, build_bm25_index_from_store
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.vector_store import VectorStore

__all__ = [
    "DEFAULT_EMBEDDING_PROVIDER_NAME",
    "DEFAULT_LLM_PROVIDER_NAME",
    "get_bm25_index",
    "get_embedding_registry",
    "get_llm_registry",
    "get_rerank_provider",
    "get_settings",
    "get_vector_store",
]

_bm25_index_cache: BM25Index | None = None
_bm25_index_lock = asyncio.Lock()


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_embedding_registry(
    settings: Settings = Depends(get_settings),
) -> EmbeddingRegistry:
    return build_embedding_registry_from_settings(settings)


def get_llm_registry(
    settings: Settings = Depends(get_settings),
) -> ProviderRegistry:
    return build_llm_registry_from_settings(settings)


def get_vector_store(
    settings: Settings = Depends(get_settings),
) -> VectorStore:
    return build_vector_store_from_settings(settings)


async def get_bm25_index(
    vector_store: VectorStore = Depends(get_vector_store),
) -> BM25Index:
    """进程内单例：首次请求时从向量库全量重建，此后复用，避免逐请求全量扫描。

    这意味着摄取新文档后，运行中的 API 进程不会实时感知到——需要重启服务
    才能让新文档进入 BM25 检索范围，这是当前 MVP 阶段接受的已知折衷。
    """
    global _bm25_index_cache
    if _bm25_index_cache is None:
        async with _bm25_index_lock:
            if _bm25_index_cache is None:
                _bm25_index_cache = await build_bm25_index_from_store(
                    vector_store
                )
    return _bm25_index_cache


def get_rerank_provider(
    settings: Settings = Depends(get_settings),
) -> RerankProvider | None:
    return build_rerank_provider_from_settings(settings)
