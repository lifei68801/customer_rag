from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.providers.base import ProviderCapability
from app.providers.openai_compatible import OpenAICompatibleChatProvider
from app.providers.registry import ProviderRegistry


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    # 见 LLMSettings.enable_thinking 的说明。
    enable_thinking: bool = False


def build_llm_registry(
    configs: list[ProviderConfig],
    *,
    client: httpx.AsyncClient | None = None,
) -> ProviderRegistry:
    """按配置逐个注册 LLM provider，共用同一个 HTTP client。"""
    registry = ProviderRegistry()
    for cfg in configs:
        registry.register(
            ProviderCapability.LLM,
            cfg.name,
            OpenAICompatibleChatProvider(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=cfg.model,
                client=client,
                enable_thinking=cfg.enable_thinking,
            ),
        )
    return registry
