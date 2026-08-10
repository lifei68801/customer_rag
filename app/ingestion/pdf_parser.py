from __future__ import annotations

import asyncio
import tempfile
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


def _clean_cell(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def _looks_numeric(value: str) -> bool:
    stripped = value.replace(",", "").replace("%", "").replace("-", "").strip()
    if not stripped:
        return False
    try:
        float(stripped)
        return True
    except ValueError:
        return False


def _looks_like_header_row(row: list[str]) -> bool:
    """启发式判断一行是不是"表头/分区标题行"，而不是数据行：非空单元格
    数量很少（<=2——分区标题通常只有 1 个非空值，子表头通常有 2 个：
    名称列表头 + 数值列表头），且没有一个单元格看起来像数字（数据行
    至少会有一个数值列，表头/标题不会）。
    """
    non_empty = [v for v in row if v]
    if not non_empty or len(non_empty) > 2:
        return False
    return not any(_looks_numeric(v) for v in non_empty)


def _split_table_into_sections(
    rows: list[list[str]],
) -> list[tuple[list[str], list[list[str]]]]:
    """财务报表里常见的"一个表格框里塞好几个逻辑分区"布局（比如员工构成
    表：基础统计 5 行 → "专业构成"分区标题 → 子表头 → 7 行数据 →
    "教育程度"分区标题 → 子表头 → "博士 818"）——如果整张表死用第一行
    当全局表头，后面分区的数据行会被错误地和不相关的表头拼在一起。
    2026-08-10 排查过一个真实案例：某财报"博士"这一行被错误拼接成了
    "16,233：818"，"博士"这个关键词完全丢失，语义检索命中不到。

    按 _looks_like_header_row 识别出的表头/分区标题行切分成多个
    (headers, data_rows) 子表；连续出现多个表头行（分区标题紧跟着自己
    的子表头）时，只有最后一个真正生效——因为分区标题行本身没有数据
    （current_data_rows 为空），不会被当成一个独立 section 保留下来，
    效果上等价于"用离数据行最近的表头行"。

    current_headers 初始为 []（不是 None）：表格开头、在遇到第一个表头
    行之前出现的数据行（比如财报里常见的"先罗列几条汇总统计，再进入
    分区表格"）也要被保留成一个 section，headers=[]，_table_chunks_for_page
    对这类"没有关联表头"的行会退化成直接拼接非空值——不能因为还没遇到
    表头就把这些数据行丢弃。
    """
    sections: list[tuple[list[str], list[list[str]]]] = []
    current_headers: list[str] = []
    current_data_rows: list[list[str]] = []
    for row in rows:
        if _looks_like_header_row(row):
            if current_data_rows:
                sections.append((current_headers, current_data_rows))
            current_headers = row
            current_data_rows = []
        else:
            current_data_rows.append(row)
    if current_data_rows:
        sections.append((current_headers, current_data_rows))
    return sections


def _table_chunks_for_page(fitz_page, *, page_number: int, source: str) -> list[Chunk]:
    """用 PyMuPDF 的版面分析找出页面里的表格，为每个表格（可能会先按
    _split_table_into_sections 切分成多个逻辑子表）生成 parent-child 两级
    chunk：子表的完整文本作为 parent_text（命中后返回给 LLM 的完整上下文，
    避免表格被拆散丢失语义），表头+单行数据拼接后的文本作为每一行的
    embedding 目标（child）——真实财报类文档验证过，按整页或整张表做
    embedding 会让"某个具体实体的某个具体属性"这类问题的检索精度被一
    整页/一整表的平均语义稀释掉。

    只有 1 行（纯表头、没有数据行）或数据行全空的表格直接跳过，不生成
    毫无信息量的 chunk。
    """
    chunks: list[Chunk] = []
    for table in fitz_page.find_tables().tables:
        rows = [[_clean_cell(cell) for cell in row] for row in table.extract()]
        if len(rows) < 2:
            continue

        for headers, data_rows in _split_table_into_sections(rows):
            non_empty_data_rows = [row for row in data_rows if any(row)]
            if not non_empty_data_rows:
                continue

            parent_lines = [" | ".join(headers)] + [
                " | ".join(row) for row in non_empty_data_rows
            ]
            parent_text = "\n".join(parent_lines)

            for row in non_empty_data_rows:
                paired_parts = [
                    f"{header}：{value}"
                    for header, value in zip(headers, row)
                    if header and value
                ]
                row_non_empty_count = sum(1 for v in row if v)
                if len(paired_parts) < row_non_empty_count:
                    # 表头配对丢了这一行的部分非空值——不只是"完全配不上"
                    # 才退化，只要配对结果比这一行本身的非空值少，就说明
                    # 列对齐发生了漂移（比如某个数值列配对到了空表头，
                    # 被过滤掉了），退化成直接拼接该行所有非空值，保证不
                    # 丢数据，总比丢内容或者拼出语义错乱的文本好。
                    row_text = " ".join(v for v in row if v)
                else:
                    row_text = "；".join(paired_parts)
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


def _prepare_pdf_sync(
    path: Path, *, needs_ocr: bool, render_dpi: int, tmp_dir: Path
) -> tuple[list[str], list[int], dict[int, Path], list[list[Chunk]]]:
    """在一个线程里完成所有需要 fitz_doc（MuPDF 的 C 层对象）的同步/CPU
    密集工作：提取原生文字层、把无文字层的页面渲染成图片、检测每页表格。

    全程只在这一个线程里打开/使用/关闭 fitz_doc——MuPDF 对同一个
    Document 的并发/跨线程访问不是安全操作，所以不能把"渲染"和"表格
    检测"拆到不同的 asyncio.to_thread 调用里（那样可能被派发到不同的
    工作线程）。真正值得并发、且和 fitz_doc 无关的只有"已经落盘的图片
    发 OCR 请求"这一步，交给 parse_pdf() 里的 asyncio.gather 处理。

    返回 (page_texts, ocr_page_indexes, png_paths, table_chunks_by_page)：
    page_texts 里 ocr_page_indexes 对应的位置是占位空字符串，调用方 await
    完 OCR 后再回填。
    """
    import fitz
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    fitz_doc = fitz.open(str(path))
    try:
        page_texts: list[str] = []
        ocr_page_indexes: list[int] = []
        png_paths: dict[int, Path] = {}
        table_chunks_by_page: list[list[Chunk]] = []

        for page_index, page in enumerate(reader.pages):
            text = page.extract_text().strip()
            page_texts.append(text)
            has_text_layer = bool(text)
            if not has_text_layer and needs_ocr:
                ocr_page_indexes.append(page_index)
                pixmap = fitz_doc[page_index].get_pixmap(dpi=render_dpi)
                png_path = tmp_dir / f"page_{page_index}.png"
                pixmap.save(str(png_path))
                png_paths[page_index] = png_path
            if has_text_layer:
                # find_tables() 是基于 PDF 页面自身的矢量内容（文字块、绘制的
                # 线条）做版面分析，不是基于图片像素做视觉识别——没有文字层
                # 的页面（pypdf 判定为空）同样没有这些矢量对象可供分析，跑
                # 这一步对这类页面必然是白跑。用真实业务文档验证过（3 份
                # 财报 PDF，共 13 页扫描页）：无文字层的页面从未检测出过
                # 表格，重叠为 0，见 2026-08-10 的排查记录。跳过能省下这部分
                # 页面约 20% 的准备阶段耗时。
                #
                # 已知边界情况：如果某份 PDF 是"扫描图片背景 + 叠加矢量画的
                # 表格线框"，会同时满足"无文字层"和"find_tables() 本可以测
                # 出表格"，跳过会真的丢这类页面的表格数据——目前样本里没
                # 遇到，接受这个小概率风险。
                table_chunks_by_page.append(
                    _table_chunks_for_page(
                        fitz_doc[page_index], page_number=page_index + 1, source=str(path)
                    )
                )
            else:
                table_chunks_by_page.append([])
        return page_texts, ocr_page_indexes, png_paths, table_chunks_by_page
    finally:
        fitz_doc.close()


async def parse_pdf(
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

    表格检测只对有原生文字层的页面跑：find_tables() 分析的是页面自身的
    矢量内容，没有文字层的页面（扫描件）同样没有可分析的矢量表格结构，
    见 _prepare_pdf_sync 里的说明和已知边界情况。

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

    分两个阶段：先用 asyncio.to_thread 跑一次同步准备（文字提取+渲染+
    表格检测，见 _prepare_pdf_sync），不占用事件循环；再用
    asyncio.Semaphore 限并发、asyncio.gather 并发跑 OCR（纯网络 I/O，
    和 fitz_doc 无关，可以安全并发）。某一页 OCR 抛异常时，
    asyncio.gather 默认行为（不传 return_exceptions）会让异常直接从
    parse_pdf() 抛出，整份文件的解析失败——和串行版本的语义一致，不会
    静默丢页。
    """
    needs_ocr = ocr is not None
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        page_texts, ocr_page_indexes, png_paths, table_chunks_by_page = (
            await asyncio.to_thread(
                _prepare_pdf_sync,
                path,
                needs_ocr=needs_ocr,
                render_dpi=render_dpi,
                tmp_dir=tmp_dir,
            )
        )

        if ocr_page_indexes:
            assert ocr is not None  # needs_ocr=True 时才会有非空 ocr_page_indexes
            semaphore = asyncio.Semaphore(max_concurrency)

            async def _bounded_ocr(page_index: int) -> tuple[int, str]:
                async with semaphore:
                    text = await ocr(png_paths[page_index])
                return page_index, text

            results = await asyncio.gather(
                *(_bounded_ocr(page_index) for page_index in ocr_page_indexes)
            )
            for page_index, text in results:
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
        chunks.extend(table_chunks_by_page[page_index])
    return chunks
