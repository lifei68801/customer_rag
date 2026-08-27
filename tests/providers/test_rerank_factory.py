from app.providers.rerank_factory import build_rerank_provider_from_settings
from tests.settings_factory import build_settings


def test_returns_none_when_rerank_not_configured():
    settings = build_settings()

    provider = build_rerank_provider_from_settings(settings)

    assert provider is None


def test_returns_provider_when_rerank_configured():
    settings = build_settings(
        rerank_base_url="https://dashscope.aliyuncs.com/api/v1",
        rerank_api_key="rerank-key",
        rerank_model="gte-rerank",
    )

    provider = build_rerank_provider_from_settings(settings)

    assert provider is not None
