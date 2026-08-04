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
