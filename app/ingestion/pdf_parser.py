from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.ingestion.chunking import Chunk
from app.ingestion.ocr_parser import OcrFunction


_DEFAULT_OCR_RENDER_DPI = 200
"""PyMuPDF get_pixmap() 不传参数默认是 72 DPI（等同 PDF 原始点距 1:1），对
公式的上下标、数字后缀单位（如"671B"里的"B"）这类细小笔画来说分辨率不够，
视觉模型容易看漏/看错——实测同一页在 72 DPI 下把"671B"识别成"6718"，公式
下标大量丢失，200 DPI 能显著改善（见 2026-08-10 的 OCR 精度排查）。

只是 parse_pdf() 的 render_dpi 参数没传时的兜底值；调用方（如
ingestion_queue.py）应该优先传 Settings.ocr_render_dpi，不同供应商/账号
需要的分辨率不一定一样。"""

_DEFAULT_OCR_MAX_CONCURRENCY = 8
"""扫描件 PDF 逐页 OCR 原来是严格串行的一页一页发请求，106 页这种量级
实测要跑 1.5-2 小时以上——OCR 调用本身是网络 I/O 为主，串行毫无必要。

这个数字不是靠 RPM/TPM 算出来的——百炼账号的 RPM 6000、TPM 3000万在
任何合理并发下都有巨大冗余，从来没被真正测试到过。是给每次调用打
时间戳、实测控制变量测出来的（2026-08-10 的并发排查，同一份 20 页
文档在不同并发数下的总耗时）：并发4 = 52.4s，并发6 = 40.2s，
并发8 = 37.9s（最快），并发20 = 42.6s。时间戳证实并发20时20个请求
确实同时发出（max_concurrent=20，不是代码没并行），但服务端明显在
排队：前5-6个请求3.5-6秒内返回，之后的请求延迟递增，最慢的单次拖到
40+秒——这是账号侧未公开的并发排队限制，不是 RPM/TPM，也不是本地
代码 bug。8 是这几个测试点里最快的，继续往上加并发只会让更多请求
排到队尾，不会更快。这个"甜蜜点"因账号/供应商而异，换了 OCR 供应商
或者账号配额调整后需要重新实测，所以只是 parse_pdf() 的 max_concurrency
参数没传时的兜底值，调用方应该优先传 Settings.ocr_max_concurrency。"""


def _render_page_to_png(
    fitz_doc, page_index: int, *, tmp_dir: Path, dpi: int
) -> Path:
    """把扫描件页面渲染成图片（PyMuPDF，无需 poppler 等系统级二进制依赖）。

    只做渲染，不在这里调用 OCR：渲染要用到 fitz_doc（MuPDF 的 C 层对象），
    MuPDF 对同一个 Document 的并发访问不是线程安全的，所以渲染这一步必须
    留在调用方的单线程循环里；真正耗时、且相互独立、可以安全并发的是
    "拿着已经落盘的图片文件去跑 OCR"这一步，见 parse_pdf() 里的线程池。
    """
    pixmap = fitz_doc[page_index].get_pixmap(dpi=dpi)
    png_path = tmp_dir / f"page_{page_index}.png"
    pixmap.save(str(png_path))
    return png_path


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


def parse_pdf(
    path: Path,
    *,
    ocr: OcrFunction | None = None,
    render_dpi: int = _DEFAULT_OCR_RENDER_DPI,
    max_concurrency: int = _DEFAULT_OCR_MAX_CONCURRENCY,
) -> list[Chunk]:
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
    直接跳过该页——不强制要求调用方总是提供 OCR 函数。render_dpi/
    max_concurrency 都有默认值（见 _DEFAULT_OCR_RENDER_DPI/
    _DEFAULT_OCR_MAX_CONCURRENCY），生产调用方（ingestion_queue.py）会
    传 Settings.ocr_render_dpi/ocr_max_concurrency，不同供应商/账号的
    最优值不一定一样。

    需要 OCR 的页面分两阶段处理：先在主线程里把所有待 OCR 页面顺序渲染成
    PNG（必须单线程，MuPDF 对同一个 Document 并发访问不安全），再用限并发
    的线程池对这些已经落盘的图片并发跑 OCR（见 max_concurrency 参数的
    说明）——网络 I/O 为主的调用完全没必要挨个等，某一页 OCR 抛异常时
    整份文件的解析直接失败（和之前串行版本的语义一致，不会静默丢页）。
    """
    import fitz
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    fitz_doc = fitz.open(str(path))
    try:
        page_texts: list[str] = []
        ocr_page_indexes: list[int] = []
        for page in reader.pages:
            text = page.extract_text().strip()
            page_texts.append(text)
            if not text and ocr is not None:
                ocr_page_indexes.append(len(page_texts) - 1)

        if ocr_page_indexes:
            with tempfile.TemporaryDirectory() as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                png_paths = {
                    page_index: _render_page_to_png(
                        fitz_doc, page_index, tmp_dir=tmp_dir, dpi=render_dpi
                    )
                    for page_index in ocr_page_indexes
                }
                with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
                    ocr_results = dict(
                        zip(
                            ocr_page_indexes,
                            executor.map(
                                lambda idx: ocr(png_paths[idx]),
                                ocr_page_indexes,
                            ),
                        )
                    )
                for page_index, text in ocr_results.items():
                    page_texts[page_index] = text.strip()

        chunks: list[Chunk] = []
        for page_index, text in enumerate(page_texts):
            page_number = page_index + 1
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
                    fitz_doc[page_index],
                    page_number=page_number,
                    source=str(path),
                )
            )
    finally:
        fitz_doc.close()
    return chunks
