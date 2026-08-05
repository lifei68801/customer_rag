from __future__ import annotations

import httpx
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class RerankRequest:
    query: str
    documents: list[str]
    top_n: int | None = None


@dataclass(frozen=True)
class RerankHit:
    index: int
    relevance_score: float


@dataclass(frozen=True)
class RerankResult:
    hits: list[RerankHit]
    raw: dict[str, Any] = field(default_factory=dict)


class RerankProvider(Protocol):
    async def rerank(self, request: RerankRequest) -> RerankResult: ...


class GenericRerankProvider:
    """适配 {query, documents} -> {results:[{index, relevance_score}]} 形状的 rerank 接口。

    与 chat/embeddings 不同，rerank 没有 OpenAI 标准形状，DashScope/Cohere/Jina
    等主流供应商都用这套 {query, documents, top_n} 请求体，因此单独抽一个 adapter。
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

    async def rerank(self, request: RerankRequest) -> RerankResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "query": request.query,
            "documents": request.documents,
        }
        if request.top_n is not None:
            payload["top_n"] = request.top_n

        response = await self._client.post(
            f"{self._base_url}/rerank",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        hits = [
            RerankHit(
                index=item["index"], relevance_score=item["relevance_score"]
            )
            for item in body["results"]
        ]
        return RerankResult(hits=hits, raw=body)
