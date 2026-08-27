from app.providers.dashscope_tts import DashScopeTTSProvider
from app.providers.voice_factory import (
    build_asr_provider_from_settings,
    build_tts_provider_from_settings,
)
from tests.settings_factory import build_settings as _settings


def test_returns_none_when_asr_not_configured():
    settings = _settings()
    assert build_asr_provider_from_settings(settings) is None


def test_returns_provider_when_asr_configured():
    settings = _settings(
        asr_base_url="https://dashscope.aliyuncs.com/api/v1",
        asr_api_key="k",
        asr_model="paraformer-realtime-v2",
    )
    assert build_asr_provider_from_settings(settings) is not None


def test_returns_provider_when_tts_configured():
    settings = _settings(
        tts_base_url="https://dashscope.aliyuncs.com/api/v1",
        tts_api_key="k",
        tts_model="cosyvoice-v1",
    )
    assert build_tts_provider_from_settings(settings) is not None


def test_returns_dashscope_provider_when_dashscope_tts_configured():
    settings = _settings(
        tts_api_key="k",
        tts_model="cosyvoice-v3.5-flash",
        tts_dashscope_websocket_url="wss://example.com/api-ws/v1/inference",
        tts_dashscope_http_url="https://example.com/api/v1",
        tts_dashscope_voice="cloned-voice-id",
    )
    provider = build_tts_provider_from_settings(settings)
    assert isinstance(provider, DashScopeTTSProvider)


def test_prefers_dashscope_provider_over_generic_when_both_configured():
    settings = _settings(
        tts_base_url="https://dashscope.aliyuncs.com/api/v1",
        tts_api_key="k",
        tts_model="cosyvoice-v3.5-flash",
        tts_dashscope_websocket_url="wss://example.com/api-ws/v1/inference",
        tts_dashscope_http_url="https://example.com/api/v1",
        tts_dashscope_voice="cloned-voice-id",
    )
    provider = build_tts_provider_from_settings(settings)
    assert isinstance(provider, DashScopeTTSProvider)
