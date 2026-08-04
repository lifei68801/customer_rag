from __future__ import annotations

from app.providers.base import Provider, ProviderCapability, ProviderRequest, ProviderResult


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
