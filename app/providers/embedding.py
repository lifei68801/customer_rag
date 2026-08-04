from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: list[str]
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    raw: dict[str, Any] = field(default_factory=dict)


class EmbeddingProvider(Protocol):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...


class EmbeddingRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, EmbeddingProvider] = {}

    def register(self, name: str, provider: EmbeddingProvider) -> None:
        self._providers[name] = provider

    async def run(
        self,
        request: EmbeddingRequest,
        *,
        provider_name: str,
    ) -> EmbeddingResult:
        provider = self._providers.get(provider_name)
        if provider is None:
            raise KeyError(f"no embedding provider registered for name={provider_name!r}")
        return await provider.embed(request)
