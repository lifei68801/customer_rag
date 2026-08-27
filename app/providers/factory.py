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
                base_url=settings.llm.base_url,
                api_key=settings.llm.api_key,
                model=settings.llm.model,
                enable_thinking=settings.llm.enable_thinking,
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
            base_url=settings.embedding.base_url,
            api_key=settings.embedding.api_key,
            model=settings.embedding.model,
            client=client,
            batch_size=settings.embedding.batch_size,
            max_concurrency=settings.embedding.max_concurrency,
        ),
    )
    return registry
