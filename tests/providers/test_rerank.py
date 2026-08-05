import json

import httpx

from app.providers.rerank import GenericRerankProvider, RerankRequest


async def test_rerank_sends_query_and_documents_and_parses_ranked_hits():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.12},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GenericRerankProvider(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-key",
        model="gte-rerank",
        client=client,
    )

    result = await provider.rerank(
        RerankRequest(
            query="E502 错误码是什么意思",
            documents=["登录失败请检查账号密码", "错误码 E502 表示网关超时"],
            top_n=2,
        )
    )

    assert [hit.index for hit in result.hits] == [1, 0]
    assert result.hits[0].relevance_score == 0.95
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["query"] == "E502 错误码是什么意思"
    assert captured["body"]["documents"] == [
        "登录失败请检查账号密码",
        "错误码 E502 表示网关超时",
    ]
    assert captured["body"]["top_n"] == 2
