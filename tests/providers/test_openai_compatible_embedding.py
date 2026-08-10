import asyncio
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


async def test_embed_preserves_vector_order_even_when_batches_finish_out_of_order():
    # 并发下批次可能乱序完成——第一批（"a","bb"）人为拖慢，第二批
    # （"ccc","dddd"）反而先返回。下游 pipeline.py 靠位置把向量和原始
    # 文本一一对应，这里必须验证 asyncio.gather 的顺序保证真的生效：
    # 结果向量要按 texts 的原始顺序排列，不能因为完成顺序被打乱而错位
    # （错位不会报错，只会让检索结果悄悄配错向量）。
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        texts = body["input"]
        if texts == ["a", "bb"]:
            await asyncio.sleep(0.05)  # 第一批故意更慢
        return httpx.Response(
            200,
            json={"data": [{"embedding": [float(len(t))]} for t in texts]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="text-embedding-v3",
        client=client,
        batch_size=2,
        max_concurrency=3,
    )

    result = await provider.embed(
        EmbeddingRequest(texts=["a", "bb", "ccc", "dddd", "e"])
    )

    # 按原始 texts 顺序：a(1) bb(2) ccc(3) dddd(4) e(1)
    assert result.vectors == [[1.0], [2.0], [3.0], [4.0], [1.0]]


async def test_embed_respects_max_concurrency_limit():
    # 用一个计数器 + 短暂延迟追踪同时在途的请求数峰值，验证
    # max_concurrency 真的限制住了并发数，不是形同虚设的参数。
    in_flight = 0
    max_observed = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_observed
        async with lock:
            in_flight += 1
            max_observed = max(max_observed, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"embedding": [0.0]} for _ in body["input"]]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="text-embedding-v3",
        client=client,
        batch_size=1,
        max_concurrency=3,
    )

    await provider.embed(EmbeddingRequest(texts=["a", "b", "c", "d", "e", "f", "g", "h"]))

    assert max_observed == 3
