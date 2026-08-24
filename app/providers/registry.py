from __future__ import annotations

from typing import AsyncIterator

from app.providers.base import (
    Provider,
    ProviderCapability,
    ProviderRequest,
    ProviderResult,
    ProviderStreamChunk,
)


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[ProviderCapability, str], Provider] = {}

    def register(
        self,
        capability: ProviderCapability,
        name: str,
        provider: Provider,
    ) -> None:
        self._providers[(capability, name)] = provider

    async def run(
        self,
        capability: ProviderCapability,
        request: ProviderRequest,
        *,
        provider_name: str,
    ) -> ProviderResult:
        provider = self._providers.get((capability, provider_name))
        if provider is None:
            raise KeyError(
                f"no provider registered for capability={capability!r} "
                f"name={provider_name!r}"
            )
        return await provider.complete(request)

    def supports_streaming(self, capability: ProviderCapability, provider_name: str) -> bool:
        """stream_complete 不是 Provider 协议的必需方法（不是所有 provider
        都支持流式），调用方需要先查一下再决定走流式还是一次性 complete()。
        """
        provider = self._providers.get((capability, provider_name))
        return provider is not None and hasattr(provider, "stream_complete")

    async def stream(
        self,
        capability: ProviderCapability,
        request: ProviderRequest,
        *,
        provider_name: str,
    ) -> AsyncIterator[str]:
        provider = self._providers.get((capability, provider_name))
        if provider is None:
            raise KeyError(
                f"no provider registered for capability={capability!r} "
                f"name={provider_name!r}"
            )
        async for chunk in provider.stream_complete(request):
            yield chunk

    def supports_tool_streaming(self, capability: ProviderCapability, provider_name: str) -> bool:
        """跟 supports_streaming() 同一个模式：stream_complete_with_tools
        不是 Provider 协议的必需方法，调用方要先查一下再决定走流式还是
        一次性 complete()。"""
        provider = self._providers.get((capability, provider_name))
        return provider is not None and hasattr(provider, "stream_complete_with_tools")

    async def stream_with_tools(
        self,
        capability: ProviderCapability,
        request: ProviderRequest,
        *,
        provider_name: str,
    ) -> AsyncIterator[ProviderStreamChunk]:
        provider = self._providers.get((capability, provider_name))
        if provider is None:
            raise KeyError(
                f"no provider registered for capability={capability!r} "
                f"name={provider_name!r}"
            )
        async for chunk in provider.stream_complete_with_tools(request):
            yield chunk
