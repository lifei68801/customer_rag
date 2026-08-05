from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.ingestion.pdf_parser import parse_pdf

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def _write_pdf(path, pages: list[str]) -> None:
    c = canvas.Canvas(str(path))
    for text in pages:
        c.setFont("STSong-Light", 12)
        c.drawString(100, 750, text)
        c.showPage()
    c.save()


def test_parse_pdf_returns_one_chunk_per_page(tmp_path):
    pdf_path = tmp_path / "manual.pdf"
    _write_pdf(pdf_path, ["安装步骤说明", "常见故障排查"])

    chunks = parse_pdf(pdf_path)

    assert len(chunks) == 2
    assert "安装步骤说明" in chunks[0].text
    assert chunks[0].source == str(pdf_path)
    assert "常见故障排查" in chunks[1].text


def test_parse_pdf_skips_blank_pages(tmp_path):
    pdf_path = tmp_path / "with_blank.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.setFont("STSong-Light", 12)
    c.drawString(100, 750, "有内容的页面")
    c.showPage()
    c.showPage()  # 完全空白页
    c.save()

    chunks = parse_pdf(pdf_path)

    assert len(chunks) == 1
    assert "有内容的页面" in chunks[0].text


def _write_image_only_pdf(pdf_path, tmp_path) -> None:
    from PIL import Image

    image_path = tmp_path / "scan.png"
    Image.new("RGB", (100, 100), color="white").save(image_path)

    c = canvas.Canvas(str(pdf_path))
    c.drawImage(str(image_path), 100, 700, width=100, height=100)
    c.showPage()
    c.save()


def test_parse_pdf_without_ocr_skips_pages_with_no_text_layer(tmp_path):
    # 扫描件 PDF：页面本身是图片、没有文字层，pypdf 提取不到任何文本
    pdf_path = tmp_path / "scanned.pdf"
    _write_image_only_pdf(pdf_path, tmp_path)

    chunks = parse_pdf(pdf_path)

    assert chunks == []


def test_parse_pdf_uses_ocr_fallback_for_pages_with_no_text_layer(tmp_path):
    pdf_path = tmp_path / "scanned.pdf"
    _write_image_only_pdf(pdf_path, tmp_path)

    def fake_ocr(path):
        return "扫描件识别出的文字"

    chunks = parse_pdf(pdf_path, ocr=fake_ocr)

    assert len(chunks) == 1
    assert chunks[0].text == "扫描件识别出的文字"
    assert chunks[0].source == str(pdf_path)
