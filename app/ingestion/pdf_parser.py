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


def _clean_cell(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def _table_chunks_for_page(fitz_page, *, page_number: int, source: str) -> list[Chunk]:
    """用 PyMuPDF 的版面分析找出页面里的表格，为每个表格生成 parent-child
    两级 chunk：整张表的完整文本作为 parent_text（命中后返回给 LLM 的
    完整上下文，避免表格被拆散丢失语义），表头+单行数据拼接后的文本
    作为每一行的 embedding 目标（child）——真实财报类文档验证过，按整页
    或整张表做 embedding 会让"某个具体实体的某个具体属性"这类问题的
    检索精度被一整页/一整表的平均语义稀释掉。

    只有 1 行（纯表头、没有数据行）或数据行全空的表格直接跳过，不生成
    毫无信息量的 chunk。
    """
    chunks: list[Chunk] = []
    for table in fitz_page.find_tables().tables:
        rows = [[_clean_cell(cell) for cell in row] for row in table.extract()]
        if len(rows) < 2:
            continue
        headers, data_rows = rows[0], rows[1:]
        non_empty_data_rows = [row for row in data_rows if any(row)]
        if not non_empty_data_rows:
            continue

        parent_lines = [" | ".join(headers)] + [
            " | ".join(row) for row in non_empty_data_rows
        ]
        parent_text = "\n".join(parent_lines)

        for row in non_empty_data_rows:
            row_text = "；".join(
                f"{header}：{value}"
                for header, value in zip(headers, row)
                if header and value
            )
            if not row_text:
                continue
            chunks.append(
                Chunk(
                    text=row_text,
                    heading_path=[f"第{page_number}页", "表格"],
                    source=source,
                    parent_text=parent_text,
                )
            )
    return chunks


def parse_pdf(path: Path, *, ocr: OcrFunction | None = None) -> list[Chunk]:
    """逐页提取 PDF 文本，每页作为一个 chunk；额外用 PyMuPDF 检测每页的
    表格，为表格数据行生成 parent-child chunk（见 _table_chunks_for_page）
    ——两者是叠加关系，整页文本 chunk 不因为页面里含表格就跳过表格区域，
    表格内容会同时出现在"整页粗粒度 chunk"和"表格行细粒度 chunk"里，
    这点重复没有坏处，只是多了一条能精确命中的检索路径。

    PDF 纯文本提取不含可靠的标题层级标记（不同于 Markdown 的 `## `），
    因此非表格内容的 heading_path 仅记录页码用于溯源，不做真正的结构
    感知分块。

    ocr 为可选项：某一页提取不到文字层（扫描件 PDF，页面本身是图片）
    时，提供了 ocr 才会把该页渲染成图片再走 OCR；不提供则保持原有行为，
    直接跳过该页——不强制要求调用方总是提供 OCR 函数。
    """
    import fitz
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    fitz_doc = fitz.open(str(path))
    chunks: list[Chunk] = []
    try:
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text().strip()
            if not text and ocr is not None:
                text = _ocr_render_page(fitz_doc, page_number - 1, ocr=ocr).strip()
            if text:
                chunks.append(
                    Chunk(
                        text=text,
                        heading_path=[f"第{page_number}页"],
                        source=str(path),
                    )
                )
            chunks.extend(
                _table_chunks_for_page(
                    fitz_doc[page_number - 1],
                    page_number=page_number,
                    source=str(path),
                )
            )
    finally:
        fitz_doc.close()
    return chunks
