from __future__ import annotations

import tempfile
from pathlib import Path

from app.ingestion.chunking import Chunk
from app.ingestion.ocr_parser import OcrFunction


def _ocr_render_page(fitz_doc, page_index: int, *, ocr: OcrFunction) -> str:
    """把扫描件页面渲染成图片（PyMuPDF，无需 poppler 等系统级二进制依赖），
    落一个临时 PNG 文件后复用现有的 OcrFunction 接口——OCR 函数只认文件
    路径，不需要为"内存渲染的页面"单独定义一套接口。
    """
    pixmap = fitz_doc[page_index].get_pixmap()
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "page.png"
        pixmap.save(str(tmp_path))
        return ocr(tmp_path)


def parse_pdf(path: Path, *, ocr: OcrFunction | None = None) -> list[Chunk]:
    """逐页提取 PDF 文本，每页作为一个 chunk。

    PDF 纯文本提取不含可靠的标题层级标记（不同于 Markdown 的 `## `），
    因此 heading_path 仅记录页码用于溯源，不做真正的结构感知分块；
    需要标题层级时应依赖排版分析/OCR，属于后续工作。

    ocr 为可选项：某一页提取不到文字层（扫描件 PDF，页面本身是图片）
    时，提供了 ocr 才会把该页渲染成图片再走 OCR；不提供则保持原有行为，
    直接跳过该页——不强制要求调用方总是提供 OCR 函数。
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks: list[Chunk] = []
    fitz_doc = None
    try:
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text().strip()
            if not text and ocr is not None:
                if fitz_doc is None:
                    import fitz

                    fitz_doc = fitz.open(str(path))
                text = _ocr_render_page(fitz_doc, page_number - 1, ocr=ocr).strip()
            if not text:
                continue
            chunks.append(
                Chunk(
                    text=text,
                    heading_path=[f"第{page_number}页"],
                    source=str(path),
                )
            )
    finally:
        if fitz_doc is not None:
            fitz_doc.close()
    return chunks
