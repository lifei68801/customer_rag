from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class ProviderCapability(Enum):
    LLM = "llm"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串，和 OpenAI tool_calls[i].function.arguments 一致


@dataclass(frozen=True)
class ProviderRequest:
    messages: list[dict[str, str]]
    options: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderResult:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] | None = None


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResult: ...
