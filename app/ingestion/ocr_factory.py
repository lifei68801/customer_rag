from __future__ import annotations

from app.config.settings import Settings
from app.ingestion.dashscope_ocr import build_dashscope_ocr
from app.ingestion.ocr_parser import OcrFunction


def build_ocr_from_settings(settings: Settings) -> OcrFunction | None:
    """OCR 为可选项，base_url/api_key 任一缺失则返回 None，调用方（parse_pdf/
    parse_image）在拿到 None 时对无文字层的页面/图片直接跳过，不报错。
    """
    if not (settings.ocr.base_url and settings.ocr.api_key):
        return None
    return build_dashscope_ocr(
        base_url=settings.ocr.base_url,
        api_key=settings.ocr.api_key,
        model=settings.ocr.model,
    )
