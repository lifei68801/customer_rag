import asyncio

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.ingestion.pdf_parser import _table_chunks_for_page, parse_pdf

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


class _FakeTable:
    def __init__(self, rows: list[list[str | None]]) -> None:
        self._rows = rows

    def extract(self):
        return self._rows


class _FakeTableFinder:
    def __init__(self, tables: list[_FakeTable]) -> None:
        self.tables = tables


class _FakePage:
    """模拟 fitz_page.find_tables().tables[i].extract()，不需要真的渲染
    PDF——用于直接喂给 _table_chunks_for_page 真实的、从有问题的财报
    PDF 里导出的原始表格行数据（见下面的多分区表格测试）。
    """

    def __init__(self, tables_rows: list[list[list[str | None]]]) -> None:
        self._tables_rows = tables_rows

    def find_tables(self):
        return _FakeTableFinder([_FakeTable(rows) for rows in self._tables_rows])


def _write_pdf(path, pages: list[str]) -> None:
    c = canvas.Canvas(str(path))
    for text in pages:
        c.setFont("STSong-Light", 12)
        c.drawString(100, 750, text)
        c.showPage()
    c.save()


async def test_parse_pdf_returns_one_chunk_per_page(tmp_path):
    pdf_path = tmp_path / "manual.pdf"
    _write_pdf(pdf_path, ["安装步骤说明", "常见故障排查"])

    chunks = await parse_pdf(pdf_path)

    assert len(chunks) == 2
    assert "安装步骤说明" in chunks[0].text
    assert chunks[0].source == str(pdf_path)
    assert "常见故障排查" in chunks[1].text


async def test_parse_pdf_skips_blank_pages(tmp_path):
    pdf_path = tmp_path / "with_blank.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.setFont("STSong-Light", 12)
    c.drawString(100, 750, "有内容的页面")
    c.showPage()
    c.showPage()  # 完全空白页
    c.save()

    chunks = await parse_pdf(pdf_path)

    assert len(chunks) == 1
    assert "有内容的页面" in chunks[0].text


def _write_image_only_pdf(pdf_path, tmp_path, *, pages: int = 1) -> None:
    from PIL import Image

    image_path = tmp_path / "scan.png"
    Image.new("RGB", (100, 100), color="white").save(image_path)

    c = canvas.Canvas(str(pdf_path))
    for _ in range(pages):
        c.drawImage(str(image_path), 100, 700, width=100, height=100)
        c.showPage()
    c.save()


async def test_parse_pdf_without_ocr_skips_pages_with_no_text_layer(tmp_path):
    # 扫描件 PDF：页面本身是图片、没有文字层，pypdf 提取不到任何文本
    pdf_path = tmp_path / "scanned.pdf"
    _write_image_only_pdf(pdf_path, tmp_path)

    chunks = await parse_pdf(pdf_path)

    assert chunks == []


async def test_parse_pdf_uses_ocr_fallback_for_pages_with_no_text_layer(tmp_path):
    pdf_path = tmp_path / "scanned.pdf"
    _write_image_only_pdf(pdf_path, tmp_path)

    async def fake_ocr(path):
        return "扫描件识别出的文字"

    chunks = await parse_pdf(pdf_path, ocr=fake_ocr)

    assert len(chunks) == 1
    assert chunks[0].text == "扫描件识别出的文字"
    assert chunks[0].source == str(pdf_path)


async def test_parse_pdf_ocr_preserves_page_order_under_concurrency(tmp_path):
    # OCR 现在是 asyncio.Semaphore 限并发跑的（见 pdf_parser.py 的
    # _DEFAULT_OCR_MAX_CONCURRENCY），完成顺序可能和页码顺序不一致——用
    # 能区分调用顺序的 fake_ocr 验证最终拼回 chunk 列表时页码顺序仍然
    # 正确，不会因为并发而错位。
    pdf_path = tmp_path / "scanned_multi.pdf"
    _write_image_only_pdf(pdf_path, tmp_path, pages=6)

    call_count = {"n": 0}

    async def fake_ocr(path):
        call_count["n"] += 1
        return f"第{call_count['n']}次被调用识别出的文字"

    chunks = await parse_pdf(pdf_path, ocr=fake_ocr)

    assert len(chunks) == 6
    assert call_count["n"] == 6
    for i, chunk in enumerate(chunks, start=1):
        assert chunk.heading_path == [f"第{i}页"]


async def test_parse_pdf_ocr_failure_on_one_page_propagates(tmp_path):
    pdf_path = tmp_path / "scanned_multi.pdf"
    _write_image_only_pdf(pdf_path, tmp_path, pages=3)

    async def flaky_ocr(path):
        if "page_1" in str(path):
            raise RuntimeError("OCR 供应商超时")
        return "识别出的文字"

    try:
        await parse_pdf(pdf_path, ocr=flaky_ocr)
    except RuntimeError as exc:
        assert "超时" in str(exc)
    else:
        raise AssertionError("单页 OCR 失败应该让整份文件的解析失败，不能被静默吞掉")


def _write_pdf_with_table(pdf_path, rows: list[list[str]]) -> None:
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    doc = SimpleDocTemplate(str(pdf_path))
    table = Table(rows)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
            ]
        )
    )
    doc.build([table])


async def test_parse_pdf_creates_parent_child_chunks_for_table_rows(tmp_path):
    pdf_path = tmp_path / "table.pdf"
    _write_pdf_with_table(
        pdf_path,
        [
            ["股东名称", "持股比例"],
            ["武汉金融控股", "75.00%"],
            ["东亚银行", "15.38%"],
        ],
    )

    chunks = await parse_pdf(pdf_path)

    table_chunks = [c for c in chunks if c.parent_text is not None]
    assert len(table_chunks) == 2
    assert "股东名称：武汉金融控股" in table_chunks[0].text
    assert "持股比例：75.00%" in table_chunks[0].text
    assert "东亚银行" in table_chunks[0].parent_text
    assert "武汉金融控股" in table_chunks[0].parent_text
    assert table_chunks[0].parent_text == table_chunks[1].parent_text
    assert table_chunks[0].source == str(pdf_path)


async def test_parse_pdf_skips_header_only_tables(tmp_path):
    pdf_path = tmp_path / "table.pdf"
    _write_pdf_with_table(pdf_path, [["股东名称", "持股比例"]])

    chunks = await parse_pdf(pdf_path)

    assert all(c.parent_text is None for c in chunks)


# 真实财报 PDF（宁德时代 2025 年年报第 56 页"员工数量、专业构成及教育
# 程度"表）导出的原始行数据，原样保留——这是 2026-08-10 排查"问'博士
# 有多少人'检索不到"这个 bug 时的真实案例：一个表格框里塞了三个逻辑
# 分区（基础统计 / 专业构成 / 教育程度），用合并单元格的分区标题行
# （"专业构成""教育程度"）和各自的子表头分隔。旧实现死用第一行当全局
# 表头，把"博士"这一行错误拼接成了"16,233：818"，"博士"这个关键词
# 完全丢失。
_NINGDE_P56_EMPLOYEE_TABLE_ROWS = [
    ["", "报告期末母公司在职员工的数量（人）", "", "16,233", None, None],
    ["", "报告期末主要子公司在职员工的数量（人）", "", "169,606", None, None],
    ["", "报告期末在职员工的数量合计（人）", "", "185,839", None, None],
    ["", "当期领取薪酬员工总人数（人）", "", "185,839", None, None],
    ["", "母公司及主要子公司需承担费用的离退休职工人数（人）", "", "0", None, None],
    ["", "专业构成", None, None, None, ""],
    ["", "专业构成类别", "", "", "专业构成人数（人）", ""],
    ["", "生产人员", "", "145,568", None, None],
    ["", "销售人员", "", "3,615", None, None],
    ["", "技术人员", "", "22,901", None, None],
    ["", "财务人员", "", "780", None, None],
    ["", "行政人员", "", "12,975", None, None],
    ["", "合计", "", "185,839", None, None],
    ["", "教育程度", None, None, None, ""],
    ["", "教育程度类别", "", "", "数量（人）", ""],
    ["博士", None, None, "818", None, None],
]


def test_table_chunks_for_page_keeps_phd_row_intact_in_multi_section_table():
    page = _FakePage([_NINGDE_P56_EMPLOYEE_TABLE_ROWS])

    chunks = _table_chunks_for_page(page, page_number=56, source="ningde.pdf")

    texts = [c.text for c in chunks]
    assert "博士 818" in texts, (
        f"'博士'这一行应该同时保留类别名和数值，实际 chunk 文本：{texts}"
    )
    # 表头前的基础统计数据（没有关联的子表头）不能被静默丢弃
    assert any("16,233" in t for t in texts)
    assert any("报告期末母公司在职员工的数量" in t for t in texts)
    # 专业构成分区的数据要配对到自己的子表头，不能混进教育程度分区
    assert any("生产人员" in t and "145,568" in t for t in texts)
    # 旧 bug 的具体错误输出不应该再出现
    assert not any("16,233：818" in t for t in texts)


async def test_parse_pdf_uses_table_extractor_result_when_table_present(tmp_path):
    pdf_path = tmp_path / "table.pdf"
    _write_pdf_with_table(
        pdf_path,
        [
            ["股东名称", "持股比例"],
            ["武汉金融控股", "75.00%"],
        ],
    )

    async def fake_table_extractor(path):
        return ["股东名称：武汉金融控股，持股比例：75.00%（视觉模型提取）"]

    chunks = await parse_pdf(pdf_path, table_extractor=fake_table_extractor)

    table_chunks = [c for c in chunks if c.parent_text is not None]
    assert len(table_chunks) == 1
    assert table_chunks[0].text == "股东名称：武汉金融控股，持股比例：75.00%（视觉模型提取）"
    assert table_chunks[0].parent_text == table_chunks[0].text
    assert table_chunks[0].source == str(pdf_path)


async def test_parse_pdf_table_extractor_not_called_for_pages_without_tables(tmp_path):
    pdf_path = tmp_path / "manual.pdf"
    _write_pdf(pdf_path, ["没有表格的普通文字页面"])
    calls: list = []

    async def fake_table_extractor(path):
        calls.append(path)
        return ["不应该被调用"]

    chunks = await parse_pdf(pdf_path, table_extractor=fake_table_extractor)

    assert calls == []
    assert all(c.parent_text is None for c in chunks)


async def test_parse_pdf_table_extractor_failure_falls_back_to_rule_based_chunks(tmp_path):
    pdf_path = tmp_path / "table.pdf"
    _write_pdf_with_table(
        pdf_path,
        [
            ["股东名称", "持股比例"],
            ["武汉金融控股", "75.00%"],
        ],
    )

    async def flaky_table_extractor(path):
        raise RuntimeError("视觉模型超时")

    chunks = await parse_pdf(pdf_path, table_extractor=flaky_table_extractor)

    table_chunks = [c for c in chunks if c.parent_text is not None]
    assert len(table_chunks) == 1, "视觉模型调用失败不应该让整份文件的解析失败，应保留规则兜底 chunk"
    assert "股东名称：武汉金融控股" in table_chunks[0].text


async def test_parse_pdf_table_extractor_empty_result_falls_back_to_rule_based_chunks(tmp_path):
    pdf_path = tmp_path / "table.pdf"
    _write_pdf_with_table(
        pdf_path,
        [
            ["股东名称", "持股比例"],
            ["武汉金融控股", "75.00%"],
        ],
    )

    async def empty_table_extractor(path):
        return []

    chunks = await parse_pdf(pdf_path, table_extractor=empty_table_extractor)

    table_chunks = [c for c in chunks if c.parent_text is not None]
    assert len(table_chunks) == 1, "视觉模型返回空列表时应保留规则兜底 chunk，不能让表格数据消失"
    assert "股东名称：武汉金融控股" in table_chunks[0].text


async def test_parse_pdf_table_extraction_preserves_page_order_under_concurrency(tmp_path):
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    pdf_path = tmp_path / "multi_table.pdf"
    doc = SimpleDocTemplate(str(pdf_path))
    flowables = []
    for i in range(4):
        table = Table([["名称", "数值"], [f"项目{i}", str(i)]])
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 1, colors.black),
                    ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                ]
            )
        )
        flowables.append(table)
        from reportlab.platypus import PageBreak

        flowables.append(PageBreak())
    doc.build(flowables)

    call_count = {"n": 0}

    async def fake_table_extractor(path):
        call_count["n"] += 1
        return [f"第{call_count['n']}次被调用提取出的表格事实"]

    chunks = await parse_pdf(pdf_path, table_extractor=fake_table_extractor)

    table_chunks = [c for c in chunks if c.parent_text is not None]
    assert len(table_chunks) == 4
    assert call_count["n"] == 4
    for i, chunk in enumerate(table_chunks, start=1):
        assert chunk.heading_path == [f"第{i}页", "表格"]


async def test_parse_pdf_runs_ocr_and_table_extraction_concurrently(tmp_path):
    """parse_pdf 把 OCR 阶段和表格提取阶段合并成一次 asyncio.gather 并发跑
    （2026-08-10），不再是 OCR 全部跑完才开始表格提取——两者作用于互斥的
    页面集合（有无文字层），互不依赖。这里用两个 asyncio.Event 互相等待
    对方先启动来证明：如果退化回顺序执行，先启动的一方会一直等不到另
    一方启动、卡到 asyncio.wait_for 超时，测试会失败而不是静默通过。
    """
    import fitz

    scanned_path = tmp_path / "_scanned_page.pdf"
    _write_image_only_pdf(scanned_path, tmp_path)
    table_path = tmp_path / "_table_page.pdf"
    _write_pdf_with_table(table_path, [["名称", "数值"], ["项目A", "1"]])

    pdf_path = tmp_path / "mixed.pdf"
    merged = fitz.open()
    with fitz.open(str(scanned_path)) as scanned_doc:
        merged.insert_pdf(scanned_doc)
    with fitz.open(str(table_path)) as table_doc:
        merged.insert_pdf(table_doc)
    merged.save(str(pdf_path))
    merged.close()

    ocr_started = asyncio.Event()
    table_started = asyncio.Event()

    async def fake_ocr(path):
        ocr_started.set()
        await asyncio.wait_for(table_started.wait(), timeout=5)
        return "扫描页文字"

    async def fake_table_extractor(path):
        table_started.set()
        await asyncio.wait_for(ocr_started.wait(), timeout=5)
        return ["名称：项目A，数值：1"]

    chunks = await parse_pdf(
        pdf_path, ocr=fake_ocr, table_extractor=fake_table_extractor
    )

    assert any("扫描页文字" in c.text for c in chunks)
    assert any(c.text == "名称：项目A，数值：1" for c in chunks)


async def test_parse_pdf_skips_table_detection_for_pages_without_text_layer(tmp_path, monkeypatch):
    # find_tables() 分析的是页面自身的矢量内容，没有文字层的页面（扫描件）
    # 同样没有可分析的矢量表格结构——用真实财报 PDF 验证过两者从不重叠
    # （见 2026-08-10 的排查记录），_prepare_pdf_sync 因此只对有文字层的
    # 页面跑表格检测。这里用一个"假装总能测出表格"的桩函数验证：真正跳过
    # 的是调用本身，不是巧合地测不出表格——否则这个测试测不出跳过逻辑
    # 是否真的生效。_prepare_pdf_sync 直接调用 _table_chunks_from_tables
    # （不经过 _table_chunks_for_page 那层薄包装，避免重复调用
    # find_tables()），所以要 patch 的是前者。
    import app.ingestion.pdf_parser as pdf_parser_module

    def fake_table_chunks_from_tables(tables, *, page_number, source):
        from app.ingestion.chunking import Chunk

        return [
            Chunk(
                text="假表格行",
                heading_path=[f"第{page_number}页", "表格"],
                source=source,
                parent_text="假表格",
            )
        ]

    monkeypatch.setattr(
        pdf_parser_module, "_table_chunks_from_tables", fake_table_chunks_from_tables
    )

    text_pdf_path = tmp_path / "text.pdf"
    _write_pdf(text_pdf_path, ["有文字层的页面"])
    scanned_pdf_path = tmp_path / "scanned.pdf"
    _write_image_only_pdf(scanned_pdf_path, tmp_path)

    text_chunks = await parse_pdf(text_pdf_path)
    scanned_chunks = await parse_pdf(scanned_pdf_path)

    assert any(c.parent_text == "假表格" for c in text_chunks), (
        "有文字层的页面应该照常跑表格检测"
    )
    assert not any(c.parent_text == "假表格" for c in scanned_chunks), (
        "没有文字层的页面应该跳过表格检测，即使 find_tables() 本可以测出表格"
    )
