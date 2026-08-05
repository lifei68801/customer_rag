from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class ASRRequest:
    audio_bytes: bytes
    audio_format: str = "wav"


@dataclass(frozen=True)
class ASRResult:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


class ASRProvider(Protocol):
    async def transcribe(self, request: ASRRequest) -> ASRResult: ...


class GenericASRProvider:
    """适配"发音频二进制、收文本"这类 ASR 接口，与 chat/embeddings 形状不同。"""

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

    async def transcribe(self, request: ASRRequest) -> ASRResult:
        response = await self._client.post(
            f"{self._base_url}/asr?model={self._model}&format={request.audio_format}",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/octet-stream",
            },
            content=request.audio_bytes,
        )
        response.raise_for_status()
        body = response.json()
        return ASRResult(text=str(body.get("text", "")), raw=body)
