from __future__ import annotations

import httpx

from app.providers.base import ProviderRequest, ProviderResult


class OpenAICompatibleChatProvider:
    """一个类适配所有 OpenAI 兼容 chat completions 接口的供应商。

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

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
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
