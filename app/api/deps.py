from __future__ import annotations

from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import VectorStore

# 默认 provider 名称，后续接入真实 Settings/环境变量配置时替换。
DEFAULT_EMBEDDING_PROVIDER_NAME = "qwen-embedding"
DEFAULT_LLM_PROVIDER_NAME = "qwen"


def get_embedding_registry() -> EmbeddingRegistry:
    raise NotImplementedError(
        "尚未接入真实 embedding provider 配置，请通过 dependency_overrides 注入"
    )


def get_vector_store() -> VectorStore:
    raise NotImplementedError(
        "尚未接入真实向量库（Milvus），请通过 dependency_overrides 注入"
    )


def get_llm_registry() -> ProviderRegistry:
    raise NotImplementedError(
        "尚未接入真实 LLM provider 配置，请通过 dependency_overrides 注入"
    )
