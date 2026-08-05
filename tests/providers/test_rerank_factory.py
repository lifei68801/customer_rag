from app.config.settings import Settings
from app.providers.rerank_factory import build_rerank_provider_from_settings


def _base_kwargs() -> dict:
    return dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
    )


def test_returns_none_when_rerank_not_configured():
    settings = Settings(**_base_kwargs())

    provider = build_rerank_provider_from_settings(settings)

    assert provider is None


def test_returns_provider_when_rerank_configured():
    settings = Settings(
        **_base_kwargs(),
        rerank_base_url="https://dashscope.aliyuncs.com/api/v1",
        rerank_api_key="rerank-key",
        rerank_model="gte-rerank",
    )

    provider = build_rerank_provider_from_settings(settings)

    assert provider is not None
