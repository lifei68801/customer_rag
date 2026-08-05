from __future__ import annotations

from pathlib import Path

from app.ingestion.chunking import Chunk


def parse_docx(path: Path) -> list[Chunk]:
    """按一级标题分块提取 Word 文档文本，每个标题下的正文作为一个 chunk。

    只识别 docx 内置的"Heading 1"样式，不做多级标题层级（Heading 2/3...
    统一并入当前一级标题的正文里）——和 pdf_parser 的"先覆盖最常见场景，
    不追求完整还原排版结构"是同一个取向。没有任何标题时整篇作为一个
    chunk，heading_path 为空（与 chunk_markdown 处理无标题文本的行为
    一致）。
    """
    from docx import Document

    document = Document(str(path))
    chunks: list[Chunk] = []
    current_heading: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        body_text = "\n".join(current_body).strip()
        if not body_text:
            return
        heading_path = [current_heading] if current_heading else []
        chunks.append(
            Chunk(text=body_text, heading_path=heading_path, source=str(path))
        )

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        if paragraph.style.name == "Heading 1":
            _flush()
            current_heading = text
            current_body = []
        else:
            current_body.append(text)
    _flush()

    return chunks
