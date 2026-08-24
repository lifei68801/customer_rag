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


import json as _json

from app.providers.base import ProviderStreamChunk, ToolCall


def _sse_body_with_tool_calls(deltas: list[dict]) -> bytes:
    """deltas 里每个元素是一个原始 choices[0].delta 字典（跳过 chunk 外层
    包装），照抄 OpenAI 流式协议的 tool_calls 分片格式：
    {"content": "..."} 或
    {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                      "function": {"name": "x", "arguments": "..."}}]}
    """
    lines = []
    for delta in deltas:
        chunk = {"choices": [{"delta": delta}]}
        lines.append(f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


async def test_stream_complete_with_tools_yields_pure_text_when_no_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_body_with_tool_calls(
                [{"content": "你好"}, {"content": "，世界"}]
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    chunks = [
        chunk
        async for chunk in provider.stream_complete_with_tools(
            ProviderRequest(messages=[{"role": "user", "content": "hi"}])
        )
    ]

    assert chunks == [
        ProviderStreamChunk(text="你好"),
        ProviderStreamChunk(text="，世界"),
    ]


async def test_stream_complete_with_tools_reconstructs_single_tool_call_from_fragments():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_body_with_tool_calls(
                [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "graph_query_tool", "arguments": ""},
                            }
                        ]
                    },
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"entity_name"'}}
                        ]
                    },
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": ': "Coca-Cola"}'}}
                        ]
                    },
                ]
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    chunks = [
        chunk
        async for chunk in provider.stream_complete_with_tools(
            ProviderRequest(messages=[{"role": "user", "content": "hi"}])
        )
    ]

    assert chunks == [
        ProviderStreamChunk(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="graph_query_tool",
                    arguments='{"entity_name": "Coca-Cola"}',
                )
            ]
        )
    ]


async def test_stream_complete_with_tools_reconstructs_two_concurrent_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_body_with_tool_calls(
                [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "vector_search_tool", "arguments": ""},
                            },
                            {
                                "index": 1,
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "graph_query_tool", "arguments": ""},
                            },
                        ]
                    },
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"query": "a"}'}},
                            {"index": 1, "function": {"arguments": '{"entity_name": "b"}'}},
                        ]
                    },
                ]
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    chunks = [
        chunk
        async for chunk in provider.stream_complete_with_tools(
            ProviderRequest(messages=[{"role": "user", "content": "hi"}])
        )
    ]

    assert chunks == [
        ProviderStreamChunk(
            tool_calls=[
                ToolCall(id="call_1", name="vector_search_tool", arguments='{"query": "a"}'),
                ToolCall(id="call_2", name="graph_query_tool", arguments='{"entity_name": "b"}'),
            ]
        )
    ]


async def test_stream_complete_with_tools_yields_leading_text_before_tool_calls():
    """决策 3 的边界情况：一轮内先有文本再改调工具，两种事件都要出现，
    顺序保留。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_body_with_tool_calls(
                [
                    {"content": "让我查一下。"},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "graph_query_tool", "arguments": "{}"},
                            }
                        ]
                    },
                ]
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    chunks = [
        chunk
        async for chunk in provider.stream_complete_with_tools(
            ProviderRequest(messages=[{"role": "user", "content": "hi"}])
        )
    ]

    assert chunks == [
        ProviderStreamChunk(text="让我查一下。"),
        ProviderStreamChunk(
            tool_calls=[ToolCall(id="call_1", name="graph_query_tool", arguments="{}")]
        ),
    ]
