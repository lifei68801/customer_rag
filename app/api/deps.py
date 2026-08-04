from __future__ import annotations

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
from app.retrieval.vector_store import VectorStore

__all__ = [
    "DEFAULT_EMBEDDING_PROVIDER_NAME",
    "DEFAULT_LLM_PROVIDER_NAME",
    "get_embedding_registry",
    "get_llm_registry",
    "get_settings",
    "get_vector_store",
]


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


def get_vector_store() -> VectorStore:
    raise NotImplementedError(
        "尚未接入真实向量库（Milvus），请通过 dependency_overrides 注入"
    )
