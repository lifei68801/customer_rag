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
    # 值类型是 Any 而不是 str——assistant 消息里的 tool_calls、tool 消息里的
    # tool_call_id 都是非字符串结构，只有 user/system 消息通常是纯字符串。
    messages: list[dict[str, Any]]
    options: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderResult:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] | None = None


@dataclass(frozen=True)
class ProviderStreamChunk:
    """stream_complete_with_tools() 的单个 yield 项。text 是本次增量文本，
    可能为空字符串（不代表流结束）；tool_calls 只会出现在流的最后一个
    chunk 上、且是完整重建好的列表——调用方不需要关心工具调用在协议里
    是按 index 分片到达、要拼接这类细节，只需要判断
    chunk.tool_calls is not None 来知道这一轮是否请求了工具调用。"""
    text: str = ""
    tool_calls: list[ToolCall] | None = None


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResult: ...
