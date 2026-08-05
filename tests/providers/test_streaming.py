import httpx

from app.providers.base import ProviderRequest
from app.providers.openai_compatible import OpenAICompatibleChatProvider


def _sse_body(deltas: list[str | None], *, include_done: bool = True) -> bytes:
    import json

    lines = []
    for delta in deltas:
        chunk = {"choices": [{"delta": {"content": delta} if delta is not None else {}}]}
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    if include_done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


async def test_stream_complete_yields_incremental_text_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(["你好", "，世界"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    deltas = [
        delta
        async for delta in provider.stream_complete(
            ProviderRequest(messages=[{"role": "user", "content": "hi"}])
        )
    ]

    assert deltas == ["你好", "，世界"]


async def test_stream_complete_sends_stream_true_in_request_body():
    import json

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, content=_sse_body(["ok"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    async for _ in provider.stream_complete(
        ProviderRequest(messages=[{"role": "user", "content": "hi"}])
    ):
        pass

    assert captured["body"]["stream"] is True


async def test_stream_complete_skips_chunks_with_no_content_delta():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(["第一句", None, "第二句"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    deltas = [
        delta
        async for delta in provider.stream_complete(
            ProviderRequest(messages=[{"role": "user", "content": "hi"}])
        )
    ]

    assert deltas == ["第一句", "第二句"]
