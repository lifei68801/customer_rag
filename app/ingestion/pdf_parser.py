from __future__ import annotations

from pathlib import Path

from app.ingestion.chunking import Chunk


def parse_pdf(path: Path) -> list[Chunk]:
    """逐页提取 PDF 文本，每页作为一个 chunk。

    PDF 纯文本提取不含可靠的标题层级标记（不同于 Markdown 的 `## `），
    因此 heading_path 仅记录页码用于溯源，不做真正的结构感知分块；
    需要标题层级时应依赖排版分析/OCR，属于后续工作。
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks: list[Chunk] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text().strip()
        if not text:
            continue
        chunks.append(
            Chunk(
                text=text,
                heading_path=[f"第{page_number}页"],
                source=str(path),
            )
        )
    return chunks
