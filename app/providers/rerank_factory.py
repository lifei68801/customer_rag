from __future__ import annotations

from app.config.settings import Settings
from app.providers.rerank import GenericRerankProvider, RerankProvider


def build_rerank_provider_from_settings(settings: Settings) -> RerankProvider | None:
    """Rerank 为可选项，三项配置任一缺失则返回 None，调用方需优雅跳过精排。"""
    if not (settings.rerank.base_url and settings.rerank.api_key and settings.rerank.model):
        return None
    return GenericRerankProvider(
        base_url=settings.rerank.base_url,
        api_key=settings.rerank.api_key,
        model=settings.rerank.model,
    )
