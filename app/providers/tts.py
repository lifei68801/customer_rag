from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class TTSRequest:
    text: str


@dataclass(frozen=True)
class TTSResult:
    audio_bytes: bytes


class TTSProvider(Protocol):
    async def synthesize(self, request: TTSRequest) -> TTSResult: ...


class GenericTTSProvider:
    """单句一次性合成：非流式，架构文档 7.3 节里的流式合成 provider

    是它的进阶版本，这里先做最基础可用的单次请求-返回音频形状。
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

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        response = await self._client.post(
            f"{self._base_url}/tts",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model, "text": request.text},
        )
        response.raise_for_status()
        return TTSResult(audio_bytes=response.content)
