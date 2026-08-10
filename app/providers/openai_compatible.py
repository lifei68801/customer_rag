from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx

from app.providers.base import ProviderRequest, ProviderResult, ToolCall
from app.providers.embedding import EmbeddingRequest, EmbeddingResult


_DEFAULT_TIMEOUT_SECONDS = 60.0


class _OpenAICompatibleClient:
    """所有 OpenAI 兼容 provider 共用的构造与鉴权逻辑。

    GLM/DeepSeek/Kimi/Qwen(DashScope) 等均提供 OpenAI 兼容模式，
    区别只在 base_url/api_key/model，无需为每家单独写 adapter。

    默认给 httpx.AsyncClient 设置 60 秒超时——httpx 自己的默认值是 5 秒，
    真实 LLM 补全（尤其带 RAG 检索上下文、或推理模型的 reasoning_content）
    经常超过这个时间，真实调用曾经因此被 ReadTimeout 打断，不是罕见的
    极端情况。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)

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
    """batch_size 为可选项：不少供应商（如阿里百炼）对单次 embeddings 请求
    能接受的文本条数有硬限制（实测 DashScope 的 text-embedding 系列限
    20 条/请求，超过直接 400），不能假设调用方一次传多少条文本就能一次
    性发出去。不设置则保持原有行为——一次性发全部文本，兼容没有这类
    限制的供应商。

    max_concurrency 控制批次间的并发度，默认 1（严格按提交顺序一批批
    等，等价于逐个 await 的老行为）——不像 OCR 那边（见 pdf_parser.py）
    已经用真实请求测过账号的并发承受能力，embedding 端点还没有实测过，
    不能假设同一账号在不同端点/模型上的限流策略一样，默认值保守到"不
    主动改变现状"，需要并发时显式调大。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        batch_size: int | None = None,
        max_concurrency: int = 1,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, model=model, client=client)
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        size = self._batch_size or len(request.texts) or 1
        batches = [
            request.texts[start : start + size]
            for start in range(0, len(request.texts), size)
        ]
        if not batches:
            return EmbeddingResult(vectors=[], raw={})

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _fetch(batch: list[str]) -> dict:
            async with semaphore:
                response = await self._client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers(),
                    json={
                        "model": self._model,
                        "input": batch,
                        **request.options,
                    },
                )
                response.raise_for_status()
                return response.json()

        # asyncio.gather 按传入协程的顺序返回结果（不是完成顺序），所以
        # 即使某个 batch 因为并发提前/滞后完成，raw_batches 仍然和
        # batches（也就是原始 texts 的切分顺序）严格一一对应——这个顺序
        # 保证是正确性的关键：下游 pipeline.py 靠 zip(chunks, vectors) 按
        # 位置把向量和文本对应起来，错位是不报错、只会让检索结果莫名其
        # 妙不准的那种 bug。
        raw_batches = await asyncio.gather(*(_fetch(batch) for batch in batches))

        vectors: list[list[float]] = []
        for body in raw_batches:
            vectors.extend(item["embedding"] for item in body["data"])

        raw: dict = {}
        if len(raw_batches) == 1:
            raw = raw_batches[0]
        elif raw_batches:
            raw = {"batches": list(raw_batches)}
        return EmbeddingResult(vectors=vectors, raw=raw)
