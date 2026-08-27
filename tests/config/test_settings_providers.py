import json

import httpx

from app.api.deps import DEFAULT_LLM_PROVIDER_NAME
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.factory import build_llm_registry_from_settings
from tests.settings_factory import build_settings


async def test_build_llm_registry_from_settings_uses_configured_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "配置生效"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = build_settings(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="settings-key",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="settings-embed-key",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
    )

    registry = build_llm_registry_from_settings(settings, client=client)
    result = await registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
        provider_name=DEFAULT_LLM_PROVIDER_NAME,
    )

    assert result.text == "配置生效"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["auth"] == "Bearer settings-key"
    assert captured["body"]["model"] == "deepseek-chat"
