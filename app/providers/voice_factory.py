from __future__ import annotations

from app.config.settings import Settings
from app.providers.asr import ASRProvider, GenericASRProvider
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
    if not (settings.tts_base_url and settings.tts_api_key and settings.tts_model):
        return None
    return GenericTTSProvider(
        base_url=settings.tts_base_url,
        api_key=settings.tts_api_key,
        model=settings.tts_model,
    )
