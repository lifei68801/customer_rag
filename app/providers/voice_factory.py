from __future__ import annotations

from app.config.settings import Settings
from app.providers.asr import ASRProvider, GenericASRProvider
from app.providers.dashscope_tts import DashScopeTTSProvider
from app.providers.tts import GenericTTSProvider, TTSProvider


def build_asr_provider_from_settings(settings: Settings) -> ASRProvider | None:
    if not (settings.asr.base_url and settings.asr.api_key and settings.asr.model):
        return None
    return GenericASRProvider(
        base_url=settings.asr.base_url,
        api_key=settings.asr.api_key,
        model=settings.asr.model,
    )


def build_tts_provider_from_settings(settings: Settings) -> TTSProvider | None:
    """dashscope 专用配置（三项都设置）优先于通用 tts_base_url——阿里百炼
    CosyVoice 走 WebSocket 协议，不是 OpenAI 兼容 REST 接口，见
    dashscope_tts.py 的说明。
    """
    if (
        settings.tts.dashscope_websocket_url
        and settings.tts.dashscope_http_url
        and settings.tts.dashscope_voice
        and settings.tts.api_key
        and settings.tts.model
    ):
        return DashScopeTTSProvider(
            api_key=settings.tts.api_key,
            model=settings.tts.model,
            voice=settings.tts.dashscope_voice,
            base_websocket_api_url=settings.tts.dashscope_websocket_url,
            base_http_api_url=settings.tts.dashscope_http_url,
        )
    if not (settings.tts.base_url and settings.tts.api_key and settings.tts.model):
        return None
    return GenericTTSProvider(
        base_url=settings.tts.base_url,
        api_key=settings.tts.api_key,
        model=settings.tts.model,
    )
