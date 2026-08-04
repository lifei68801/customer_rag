import json

import httpx

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.config import ProviderConfig, build_llm_registry


async def test_build_llm_registry_makes_every_configured_provider_routable():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": f"reply-from-{body['model']}"}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configs = [
        ProviderConfig(
            name="qwen",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="key-qwen",
            model="qwen-max",
        ),
        ProviderConfig(
            name="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key="key-deepseek",
            model="deepseek-chat",
        ),
        ProviderConfig(
            name="glm",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key="key-glm",
            model="glm-4",
        ),
        ProviderConfig(
            name="kimi",
            base_url="https://api.moonshot.cn/v1",
            api_key="key-kimi",
            model="moonshot-v1-8k",
        ),
    ]

    registry = build_llm_registry(configs, client=client)

    for cfg in configs:
        result = await registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
            provider_name=cfg.name,
        )
        assert result.text == f"reply-from-{cfg.model}"
