from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class ProviderCapability(Enum):
    LLM = "llm"


@dataclass(frozen=True)
class ProviderRequest:
    messages: list[dict[str, str]]
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResult: ...
