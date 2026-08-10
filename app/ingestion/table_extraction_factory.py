from __future__ import annotations

from app.config.settings import Settings
from app.ingestion.table_extraction import TableExtractionFunction, build_table_extractor


def build_table_extractor_from_settings(settings: Settings) -> TableExtractionFunction | None:
    """表格结构理解为可选项，base_url/api_key/model 任一缺失则返回 None，
    调用方（parse_pdf）拿到 None 时保持老行为：PyMuPDF find_tables() +
    规则猜表头。base_url/api_key 复用 OCR 那份配置（同一个百炼账号），
    model 需要单独配置——不能沿用 ocr_model（默认是专用 OCR 模型
    qwen-vl-ocr，实测会完全无视表格结构化提取指令，见
    table_extraction.py 里 _DEFAULT_MODEL 的说明）。
    """
    if not (settings.ocr_base_url and settings.ocr_api_key and settings.table_extraction_model):
        return None
    return build_table_extractor(
        base_url=settings.ocr_base_url,
        api_key=settings.ocr_api_key,
        model=settings.table_extraction_model,
    )
