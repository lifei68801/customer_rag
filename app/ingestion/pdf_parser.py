from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from app.ingestion.chunking import Chunk
from app.ingestion.ocr_parser import OcrFunction
from app.ingestion.table_extraction import TableExtractionFunction


_DEFAULT_OCR_RENDER_DPI = 200
"""PyMuPDF get_pixmap() 不传参数默认是 72 DPI（等同 PDF 原始点距 1:1），对
公式的上下标、数字后缀单位（如"671B"里的"B"）这类细小笔画来说分辨率不够，
视觉模型容易看漏/看错——实测同一页在 72 DPI 下把"671B"识别成"6718"，公式
下标大量丢失，200 DPI 能显著改善（见 2026-08-10 的 OCR 精度排查）。

只是 parse_pdf() 的 render_dpi 参数没传时的兜底值；调用方（如
ingestion_queue.py）应该优先传 OcrSettings.render_dpi，不同供应商/账号
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
参数没传时的兜底值，调用方应该优先传 OcrSettings.max_concurrency。"""

_DEFAULT_TABLE_EXTRACTION_MAX_CONCURRENCY = 40
"""2026-08-10 用真实请求对同一账号做过并发梯度实测（4/8/20/40 对比，每档
20-40 次真实调用，逐次记录时间戳核对是否有排队现象）：全程 0 个 429/
超时，且同一页在不同并发档位下耗时几乎不变（耗时由该页输出长度决定，
不是排队等待）——和 _DEFAULT_OCR_MAX_CONCURRENCY 那次测到明显排队拐点
不同，这次没有测到拐点。40 是目前测过的最高并发档位，不代表账号真实
上限就是 40（没有再往上测）。只是 parse_pdf() 的
table_extraction_max_concurrency 参数没传时的兜底值，调用方应该优先传
TableExtractionSettings.max_concurrency；换账号/供应商需要重新用同样
的方法（同一份文档、控制变量对比不同并发数）实测这个端点。"""


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


def _table_chunks_from_tables(tables, *, page_number: int, source: str) -> list[Chunk]:
    """接受已经调用过 fitz_page.find_tables().tables 拿到的表格列表——
    拆出这一层是为了让 _prepare_pdf_sync 只调用一次 find_tables()（本身
    是 CPU 密集的版面分析），同一份结果既用来判断"这一页要不要额外走
    视觉模型的表格提取"，也用来算规则兜底的 chunk，不用为了两个目的
    各调一次。

    用 PyMuPDF 的版面分析找出页面里的表格，为每个表格（可能会先按
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
    for table in tables:
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


def _table_chunks_for_page(fitz_page, *, page_number: int, source: str) -> list[Chunk]:
    """薄包装：自己调一次 find_tables()，供测试和其它只有 fitz_page、
    没有预先算好 tables 列表的调用方使用。_prepare_pdf_sync 内部走的是
    _table_chunks_from_tables，不经过这层（避免重复调用 find_tables()）。
    """
    return _table_chunks_from_tables(
        fitz_page.find_tables().tables, page_number=page_number, source=source
    )


def _prepare_pdf_sync(
    path: Path,
    *,
    needs_ocr: bool,
    needs_table_extraction: bool,
    render_dpi: int,
    tmp_dir: Path,
) -> tuple[
    list[str],
    list[int],
    dict[int, Path],
    list[list[Chunk]],
    list[int],
    dict[int, Path],
]:
    """在一个线程里完成所有需要 fitz_doc（MuPDF 的 C 层对象）的同步/CPU
    密集工作：提取原生文字层、把无文字层的页面渲染成图片、检测每页表格
    （连带渲染有表格的页面截图，供视觉模型做表格提取用）。

    全程只在这一个线程里打开/使用/关闭 fitz_doc——MuPDF 对同一个
    Document 的并发/跨线程访问不是安全操作，所以不能把"渲染"和"表格
    检测"拆到不同的 asyncio.to_thread 调用里（那样可能被派发到不同的
    工作线程）。真正值得并发、且和 fitz_doc 无关的只有"已经落盘的图片
    发请求"这一步（OCR 和表格提取都是），交给 parse_pdf() 里的
    asyncio.gather 处理。

    返回 (page_texts, ocr_page_indexes, ocr_png_paths, table_chunks_by_page,
    table_llm_page_indexes, table_llm_png_paths)：
    - page_texts 里 ocr_page_indexes 对应的位置是占位空字符串，调用方
      await 完 OCR 后再回填。
    - table_chunks_by_page 里 table_llm_page_indexes 对应的位置，是规则
      算出来的兜底结果——needs_table_extraction=True 时，调用方会尝试用
      视觉模型的结果替换掉它；视觉模型调用失败时保留这份兜底，不会因为
      一次网络请求失败就丢了整页的表格数据。
    """
    import fitz
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    fitz_doc = fitz.open(str(path))
    try:
        page_texts: list[str] = []
        ocr_page_indexes: list[int] = []
        ocr_png_paths: dict[int, Path] = {}
        table_chunks_by_page: list[list[Chunk]] = []
        table_llm_page_indexes: list[int] = []
        table_llm_png_paths: dict[int, Path] = {}

        for page_index, page in enumerate(reader.pages):
            text = page.extract_text().strip()
            page_texts.append(text)
            has_text_layer = bool(text)
            if not has_text_layer and needs_ocr:
                ocr_page_indexes.append(page_index)
                pixmap = fitz_doc[page_index].get_pixmap(dpi=render_dpi)
                png_path = tmp_dir / f"page_{page_index}.png"
                pixmap.save(str(png_path))
                ocr_png_paths[page_index] = png_path
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
                tables = fitz_doc[page_index].find_tables().tables
                table_chunks_by_page.append(
                    _table_chunks_from_tables(
                        tables, page_number=page_index + 1, source=str(path)
                    )
                )
                if needs_table_extraction and tables:
                    # 视觉模型是对整页截图做提取的（一次调用覆盖这一页
                    # 所有表格），不需要为每张表单独裁图；只有真的检测到
                    # 非空表格的页面才值得发这次请求。
                    table_llm_page_indexes.append(page_index)
                    pixmap = fitz_doc[page_index].get_pixmap(dpi=render_dpi)
                    png_path = tmp_dir / f"table_page_{page_index}.png"
                    pixmap.save(str(png_path))
                    table_llm_png_paths[page_index] = png_path
            else:
                table_chunks_by_page.append([])
        return (
            page_texts,
            ocr_page_indexes,
            ocr_png_paths,
            table_chunks_by_page,
            table_llm_page_indexes,
            table_llm_png_paths,
        )
    finally:
        fitz_doc.close()


async def parse_pdf(
    path: Path,
    *,
    ocr: OcrFunction | None = None,
    render_dpi: int = _DEFAULT_OCR_RENDER_DPI,
    max_concurrency: int = _DEFAULT_OCR_MAX_CONCURRENCY,
    table_extractor: TableExtractionFunction | None = None,
    table_extraction_max_concurrency: int = _DEFAULT_TABLE_EXTRACTION_MAX_CONCURRENCY,
    ocr_semaphore: asyncio.Semaphore | None = None,
    table_semaphore: asyncio.Semaphore | None = None,
) -> list[Chunk]:
    """逐页提取 PDF 文本，每页作为一个 chunk；额外用 PyMuPDF 检测每页的
    表格，为表格数据行生成 parent-child chunk（见 _table_chunks_from_tables）
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
    传 OcrSettings.render_dpi/ocr_max_concurrency，不同供应商/账号的
    最优值不一定一样。

    table_extractor 同样可选：提供时，检测到非空表格的页面会额外发一次
    视觉模型请求，用语义理解直接提取表格数据（见 table_extraction.py），
    比 _table_chunks_from_tables 的规则更泛化——2026-08-10 用真实财报
    文档验证过，规则处理不好的"多分区表格""逐行 key-value 简介表""双栏
    并排数据"这几类场景，视觉模型都能正确处理。不提供则保持规则兜底
    行为不变。视觉模型调用失败（网络错误/返回格式不对）时不会让整份
    文件的解析失败——跟 OCR 失败必须让整份文件失败不同（OCR 失败=那一页
    彻底没有内容），表格提取失败时规则算出来的兜底 chunk 已经在
    _prepare_pdf_sync 里备好了，静默保留兜底、只记一条 warning 日志。

    分两个阶段：先用 asyncio.to_thread 跑一次同步准备（文字提取+渲染+
    表格检测，见 _prepare_pdf_sync），不占用事件循环；再并发跑 OCR 和
    表格提取——两者作用于互斥的页面集合（OCR 只处理无文字层的页面，
    表格提取只处理有文字层的页面），彼此不读写对方的数据，天然没有
    先后依赖，2026-08-10 起改成用一个 asyncio.gather 同时发起，而不是
    OCR 全部跑完才开始表格提取，缩短两者都有工作要做的混合文档（部分
    扫描页+部分原生文字表格页）的总耗时。各自内部仍然用独立的
    asyncio.Semaphore 限并发，不共用同一个 Semaphore——分别调用不同的
    模型/端点，没理由绑在一起限流。

    某一页 OCR 抛异常时，整份文件的解析失败——和串行版本的语义一致，
    不会静默丢页；表格提取阶段内部单页失败会被捕获、不影响其它页。
    两个阶段合并后用 return_exceptions=True 等两边都跑完（不管成败）
    再手动重新抛出 OCR 的异常，而不是用 gather 默认行为——默认行为下
    一边抛异常会让 gather 立刻返回，另一边可能还在后台裸跑网络请求，
    这份 PDF 对应的临时目录马上就要被清理，会导致后台任务读到已删除
    的图片文件、报一个不会被任何人处理的"未获取异常"。

    ocr_semaphore/table_semaphore 为可选项：不传（默认）时每次调用各自
    新建一个 Semaphore，行为和之前完全一致（单文档内部按 max_concurrency/
    table_extraction_max_concurrency 限并发）。跨文档批量摄取场景下，
    process_pending_jobs() 会在处理一批任务之前构造好这两个 Semaphore
    并注入给每一份文档的 parse_pdf() 调用，让多份文档共享同一个账号级
    并发预算，而不是每份文档各自独立地把并发数用满——见
    docs/superpowers/plans/2026-08-10-qa-and-ingestion-concurrency-
    optimization.md。
    """
    needs_ocr = ocr is not None
    needs_table_extraction = table_extractor is not None
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        (
            page_texts,
            ocr_page_indexes,
            ocr_png_paths,
            table_chunks_by_page,
            table_llm_page_indexes,
            table_llm_png_paths,
        ) = await asyncio.to_thread(
            _prepare_pdf_sync,
            path,
            needs_ocr=needs_ocr,
            needs_table_extraction=needs_table_extraction,
            render_dpi=render_dpi,
            tmp_dir=tmp_dir,
        )

        async def _run_ocr_phase() -> None:
            assert ocr is not None  # needs_ocr=True 时才会有非空 ocr_page_indexes
            semaphore = ocr_semaphore or asyncio.Semaphore(max_concurrency)

            async def _bounded_ocr(page_index: int) -> tuple[int, str]:
                async with semaphore:
                    text = await ocr(ocr_png_paths[page_index])
                return page_index, text

            ocr_results = await asyncio.gather(
                *(_bounded_ocr(page_index) for page_index in ocr_page_indexes)
            )
            for page_index, text in ocr_results:
                page_texts[page_index] = text.strip()

        async def _run_table_extraction_phase() -> None:
            assert table_extractor is not None
            semaphore = table_semaphore or asyncio.Semaphore(table_extraction_max_concurrency)

            async def _bounded_table_extract(
                page_index: int,
            ) -> tuple[int, list[str] | BaseException]:
                async with semaphore:
                    try:
                        facts = await table_extractor(table_llm_png_paths[page_index])
                    except Exception as exc:  # noqa: BLE001 - 兜底已备好，不传染给调用方
                        return page_index, exc
                return page_index, facts

            table_results = await asyncio.gather(
                *(_bounded_table_extract(page_index) for page_index in table_llm_page_indexes)
            )
            for page_index, facts_or_exc in table_results:
                if isinstance(facts_or_exc, BaseException) or not facts_or_exc:
                    # 视觉模型调用失败，或者返回了空列表（比如模型没能
                    # 解析出任何数据）——保留 _prepare_pdf_sync 里已经算好
                    # 的规则兜底结果，不覆盖，不让这一页的表格数据彻底消失。
                    continue
                page_number = page_index + 1
                parent_text = "\n".join(facts_or_exc)
                table_chunks_by_page[page_index] = [
                    Chunk(
                        text=fact,
                        heading_path=[f"第{page_number}页", "表格"],
                        source=str(path),
                        parent_text=parent_text,
                    )
                    for fact in facts_or_exc
                ]

        phases = []
        if ocr_page_indexes:
            phases.append(_run_ocr_phase())
        if table_llm_page_indexes:
            phases.append(_run_table_extraction_phase())
        if phases:
            phase_results = await asyncio.gather(*phases, return_exceptions=True)
            for result in phase_results:
                if isinstance(result, BaseException):
                    raise result

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
