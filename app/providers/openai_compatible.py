from __future__ import annotations

import httpx

from app.providers.base import ProviderRequest, ProviderResult
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
        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json={
                "model": self._model,
                "messages": request.messages,
                **request.options,
            },
        )
        response.raise_for_status()
        body = response.json()
        text = body["choices"][0]["message"]["content"]
        return ProviderResult(text=text, raw=body)


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
