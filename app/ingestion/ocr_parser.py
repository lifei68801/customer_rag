from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.ingestion.chunking import Chunk

OcrFunction = Callable[[Path], str]


def _default_ocr(path: Path) -> str:
    """默认 OCR 实现，调用 pytesseract。

    需要本机额外安装 Tesseract OCR 引擎二进制——这是系统级依赖，不是
    靠 `pip install pytesseract` 能解决的（pytesseract 只是调用本机
    tesseract 可执行文件的 Python 封装）：
      - Windows: https://github.com/UB-Mannheim/tesseract/wiki
      - macOS: brew install tesseract
      - Linux (Debian/Ubuntu): apt-get install tesseract-ocr
    中文识别还需要额外装 chi_sim 语言包（多数发行版打包为
    tesseract-ocr-chi-sim）。没装的话调用这个函数会直接抛异常，不会
    静默返回空文本掩盖问题。
    """
    import pytesseract
    from PIL import Image

    with Image.open(path) as image:
        return pytesseract.image_to_string(image, lang="chi_sim+eng")


def parse_image(path: Path, *, ocr: OcrFunction | None = None) -> list[Chunk]:
    """OCR 提取图片（扫描件截图/照片等标准图片格式）里的文字，整张图作为
    一个 chunk，不做版面分析/多栏识别。

    ocr 参数可注入替换默认实现，测试时不需要本机真的装好 Tesseract。

    范围说明：只覆盖独立图片文件；"扫描件 PDF"（PDF 页面本身是图片、
    没有文字层）需要先把 PDF 页面渲染成图片（pdf2image + poppler 系统
    依赖）再走这里的 OCR，这条链路本次没有实现——不在已装的 Tesseract
    依赖之外再引入 poppler，作为明确的范围缩减记录。
    """
    ocr_fn = ocr or _default_ocr
    text = ocr_fn(path).strip()
    if not text:
        return []
    return [Chunk(text=text, heading_path=[], source=str(path))]
