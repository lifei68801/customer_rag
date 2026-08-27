from app.config.settings import Settings
from app.providers.dashscope_tts import DashScopeTTSProvider
from app.providers.voice_factory import (
    build_asr_provider_from_settings,
    build_tts_provider_from_settings,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
    )
    defaults.update(overrides)
    # _env_file=None：这批"未配置 xxx 应返回 None"的测试假定没在 defaults/
    # overrides 里出现的字段落到 Settings 类声明的默认值——但
    # Settings.model_config 声明了 env_file=".env"，不加这个覆盖的话，本地
    # 开发者 .env 里为了手动测语音功能配置的真实阿里云 TTS/ASR 凭据会静默
    # 盖过"未配置"这个前提，让"未配置 TTS 应返回 None"断言在配置了这些
    # .env 字段的开发机上失败（2026-08-27 全量测试跑排查到的根因）。
    return Settings(_env_file=None, **defaults)


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
