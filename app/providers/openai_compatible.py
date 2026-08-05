from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.providers.base import ProviderRequest, ProviderResult, ToolCall
from app.providers.embedding import EmbeddingRequest, EmbeddingResult


class _OpenAICompatibleClient:
    """所有 OpenAI 兼容 provider 共用的构造与鉴权逻辑。

    GLM/DeepSeek/Kimi/Qwen(DashScope) 等均提供 OpenAI 兼容模式，
    区别只在 base_url/api_key/model，无需为每家单独写 adapter。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}


class OpenAICompatibleChatProvider(_OpenAICompatibleClient):
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        payload: dict = {
            "model": self._model,
            "messages": request.messages,
            **request.options,
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.tool_choice:
            payload["tool_choice"] = request.tool_choice

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        message = body["choices"][0]["message"]
        # 纯工具调用轮次里 content 通常是 None，不能再假设它必是字符串。
        text = message.get("content") or ""

        tool_calls: list[ToolCall] | None = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                )
                for tc in raw_tool_calls
            ]

        return ProviderResult(text=text, raw=body, tool_calls=tool_calls)

    async def stream_complete(self, request: ProviderRequest) -> AsyncIterator[str]:
        """流式生成：逐个 yield 增量文本片段（SSE `delta.content`），不等
        完整回复生成完才返回——这是让语音输出首包延迟名副其实的前提
        （见 docs/ARCHITECTURE.md §7.3），也是 `complete()` 做不到的。

        不处理流式场景下的 tool_calls 增量拼接（工具调用的 delta 是跨多个
        chunk 拼接的片段，比纯文本流复杂得多）——流式生成目前只服务于
        语音输出这个场景，用的是静态 Responder 路径，不涉及 Planner
        工具调用；需要"流式+工具调用"两者都要的场景出现时再扩展。
        """
        payload: dict = {
            "model": self._model,
            "messages": request.messages,
            "stream": True,
            **request.options,
        }
        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: ") :]
                if data.strip() == "[DONE]":
                    break
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta


class OpenAICompatibleEmbeddingProvider(_OpenAICompatibleClient):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        response = await self._client.post(
            f"{self._base_url}/embeddings",
            headers=self._headers(),
            json={
                "model": self._model,
                "input": request.texts,
                **request.options,
            },
        )
        response.raise_for_status()
        body = response.json()
        vectors = [item["embedding"] for item in body["data"]]
        return EmbeddingResult(vectors=vectors, raw=body)
