import json

import httpx

from app.providers.base import ProviderRequest, ProviderResult, ToolCall
from app.providers.openai_compatible import OpenAICompatibleChatProvider

_SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vector_search_tool",
        "description": "在知识库里做混合检索",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def test_provider_request_accepts_tools_and_tool_choice():
    request = ProviderRequest(
        messages=[{"role": "user", "content": "hi"}],
        tools=[_SEARCH_TOOL_SCHEMA],
        tool_choice="auto",
    )

    assert request.tools == [_SEARCH_TOOL_SCHEMA]
    assert request.tool_choice == "auto"


def test_provider_request_tools_and_tool_choice_default_to_none():
    request = ProviderRequest(messages=[{"role": "user", "content": "hi"}])

    assert request.tools is None
    assert request.tool_choice is None


def test_provider_result_accepts_tool_calls():
    tool_call = ToolCall(id="call_1", name="vector_search_tool", arguments='{"query": "a"}')
    result = ProviderResult(text="", tool_calls=[tool_call])

    assert result.tool_calls == [tool_call]


def test_provider_result_tool_calls_defaults_to_none():
    result = ProviderResult(text="hello")

    assert result.tool_calls is None


async def test_complete_includes_tools_and_tool_choice_in_request_body_when_provided():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    await provider.complete(
        ProviderRequest(
            messages=[{"role": "user", "content": "hi"}],
            tools=[_SEARCH_TOOL_SCHEMA],
            tool_choice="auto",
        )
    )

    assert captured["body"]["tools"] == [_SEARCH_TOOL_SCHEMA]
    assert captured["body"]["tool_choice"] == "auto"


async def test_complete_omits_tools_from_request_body_when_not_provided():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    await provider.complete(ProviderRequest(messages=[{"role": "user", "content": "hi"}]))

    assert "tools" not in captured["body"]
    assert "tool_choice" not in captured["body"]


async def test_complete_parses_tool_calls_from_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "vector_search_tool",
                                        "arguments": '{"query": "网络故障"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    result = await provider.complete(
        ProviderRequest(
            messages=[{"role": "user", "content": "hi"}], tools=[_SEARCH_TOOL_SCHEMA]
        )
    )

    assert result.text == ""
    assert result.tool_calls == [
        ToolCall(id="call_1", name="vector_search_tool", arguments='{"query": "网络故障"}')
    ]


async def test_complete_returns_none_tool_calls_when_response_has_no_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hello"}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
        client=client,
    )

    result = await provider.complete(
        ProviderRequest(messages=[{"role": "user", "content": "hi"}])
    )

    assert result.text == "hello"
    assert result.tool_calls is None
