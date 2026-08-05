from __future__ import annotations

import httpx

from app.config.settings import Settings
from app.providers.config import ProviderConfig, build_llm_registry
from app.providers.embedding import EmbeddingRegistry
from app.providers.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.providers.registry import ProviderRegistry

# 默认 provider 名称，若未来需要支持同一能力下多个可选 provider 再扩展为可配置项。
DEFAULT_EMBEDDING_PROVIDER_NAME = "qwen-embedding"
DEFAULT_LLM_PROVIDER_NAME = "qwen"


def build_llm_registry_from_settings(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> ProviderRegistry:
    return build_llm_registry(
        [
            ProviderConfig(
                name=DEFAULT_LLM_PROVIDER_NAME,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
            )
        ],
        client=client,
    )


def build_embedding_registry_from_settings(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register(
        DEFAULT_EMBEDDING_PROVIDER_NAME,
        OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            client=client,
            batch_size=settings.embedding_batch_size,
        ),
    )
    return registry
