from __future__ import annotations

from app.config.settings import Settings
from app.providers.asr import ASRProvider, GenericASRProvider
from app.providers.dashscope_tts import DashScopeTTSProvider
from app.providers.tts import GenericTTSProvider, TTSProvider


def build_asr_provider_from_settings(settings: Settings) -> ASRProvider | None:
    if not (settings.asr_base_url and settings.asr_api_key and settings.asr_model):
        return None
    return GenericASRProvider(
        base_url=settings.asr_base_url,
        api_key=settings.asr_api_key,
        model=settings.asr_model,
    )


def build_tts_provider_from_settings(settings: Settings) -> TTSProvider | None:
    """dashscope 专用配置（三项都设置）优先于通用 tts_base_url——阿里百炼
    CosyVoice 走 WebSocket 协议，不是 OpenAI 兼容 REST 接口，见
    dashscope_tts.py 的说明。
    """
    if (
        settings.tts_dashscope_websocket_url
        and settings.tts_dashscope_http_url
        and settings.tts_dashscope_voice
        and settings.tts_api_key
        and settings.tts_model
    ):
        return DashScopeTTSProvider(
            api_key=settings.tts_api_key,
            model=settings.tts_model,
            voice=settings.tts_dashscope_voice,
            base_websocket_api_url=settings.tts_dashscope_websocket_url,
            base_http_api_url=settings.tts_dashscope_http_url,
        )
    if not (settings.tts_base_url and settings.tts_api_key and settings.tts_model):
        return None
    return GenericTTSProvider(
        base_url=settings.tts_base_url,
        api_key=settings.tts_api_key,
        model=settings.tts_model,
    )
