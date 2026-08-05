import json

import httpx

from app.providers.embedding import EmbeddingRequest
from app.providers.openai_compatible import OpenAICompatibleEmbeddingProvider


async def test_embed_sends_openai_compatible_request_and_parses_vectors():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1, 0.2]},
                    {"embedding": [0.3, 0.4]},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="text-embedding-v3",
        client=client,
    )

    result = await provider.embed(EmbeddingRequest(texts=["hello", "world"]))

    assert result.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["input"] == ["hello", "world"]


async def test_embed_splits_into_multiple_requests_when_batch_size_set():
    # 有的供应商（如阿里百炼）单次 embeddings 请求最多接受 N 条文本，
    # 超过就 400——不能假设调用方传多少条文本就能一次性发多少条。
    request_bodies: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body["input"])
        return httpx.Response(
            200,
            json={"data": [{"embedding": [float(len(text))]} for text in body["input"]]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="text-embedding-v3",
        client=client,
        batch_size=2,
    )

    result = await provider.embed(
        EmbeddingRequest(texts=["a", "bb", "ccc", "dddd", "e"])
    )

    assert request_bodies == [["a", "bb"], ["ccc", "dddd"], ["e"]]
    assert result.vectors == [[1.0], [2.0], [3.0], [4.0], [1.0]]


async def test_embed_without_batch_size_sends_everything_in_one_request():
    # 默认行为不变：不配置 batch_size 时仍然一次性发全部文本。
    request_bodies: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body["input"])
        return httpx.Response(
            200, json={"data": [{"embedding": [0.0]} for _ in body["input"]]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="text-embedding-v3",
        client=client,
    )

    await provider.embed(EmbeddingRequest(texts=["a", "b", "c"]))

    assert request_bodies == [["a", "b", "c"]]
