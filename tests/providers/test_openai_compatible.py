import httpx

from app.providers.base import ProviderRequest
from app.providers.openai_compatible import OpenAICompatibleChatProvider


async def test_complete_sends_openai_compatible_request_and_parses_response():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello from deepseek"}}]},
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

    assert result.text == "hello from deepseek"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"


def test_default_client_uses_a_generous_timeout_not_httpx_default_5s():
    # httpx.AsyncClient() 默认 5 秒超时，真实 LLM 补全（尤其带 RAG 上下文、
    # 推理模型）经常超过这个时间——真实调用曾经因此被 ReadTimeout 打断。
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-chat",
    )

    assert provider._client.timeout.read >= 60.0
