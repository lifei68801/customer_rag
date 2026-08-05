from app.config.settings import Settings
from app.providers.voice_factory import (
    build_asr_provider_from_settings,
    build_tts_provider_from_settings,
)


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


def test_returns_none_when_asr_not_configured():
    settings = Settings(**_base_kwargs())
    assert build_asr_provider_from_settings(settings) is None


def test_returns_provider_when_asr_configured():
    settings = Settings(
        **_base_kwargs(),
        asr_base_url="https://dashscope.aliyuncs.com/api/v1",
        asr_api_key="k",
        asr_model="paraformer-realtime-v2",
    )
    assert build_asr_provider_from_settings(settings) is not None


def test_returns_none_when_tts_not_configured():
    settings = Settings(**_base_kwargs())
    assert build_tts_provider_from_settings(settings) is None


def test_returns_provider_when_tts_configured():
    settings = Settings(
        **_base_kwargs(),
        tts_base_url="https://dashscope.aliyuncs.com/api/v1",
        tts_api_key="k",
        tts_model="cosyvoice-v1",
    )
    assert build_tts_provider_from_settings(settings) is not None
