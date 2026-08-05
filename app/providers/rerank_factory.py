from __future__ import annotations

from app.config.settings import Settings
from app.providers.rerank import GenericRerankProvider, RerankProvider


def build_rerank_provider_from_settings(settings: Settings) -> RerankProvider | None:
    """Rerank 为可选项，三项配置任一缺失则返回 None，调用方需优雅跳过精排。"""
    if not (settings.rerank_base_url and settings.rerank_api_key and settings.rerank_model):
        return None
    return GenericRerankProvider(
        base_url=settings.rerank_base_url,
        api_key=settings.rerank_api_key,
        model=settings.rerank_model,
    )
