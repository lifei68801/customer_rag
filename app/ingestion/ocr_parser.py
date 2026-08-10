from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from app.ingestion.chunking import Chunk

OcrFunction = Callable[[Path], Awaitable[str]]
"""异步：OCR 调用是网络 I/O（或至少是可能耗时的本地推理），parse_pdf() 需要
能对多页并发发起调用而不阻塞事件循环，所以这里统一要求 OcrFunction 返回
一个 awaitable，不是普通函数——纯同步的实现（比如下面的 _default_ocr）
自己内部用 asyncio.to_thread 包一层就行，调用方不用关心这个区别。"""


def _default_ocr_sync(path: Path) -> str:
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


async def _default_ocr(path: Path) -> str:
    """pytesseract 本身是同步阻塞调用（调本机 tesseract 可执行文件），丢进
    线程池执行，符合 OcrFunction 的异步接口，也不会占住事件循环。
    """
    return await asyncio.to_thread(_default_ocr_sync, path)


async def parse_image(path: Path, *, ocr: OcrFunction | None = None) -> list[Chunk]:
    """OCR 提取图片（扫描件截图/照片等标准图片格式）里的文字，整张图作为
    一个 chunk，不做版面分析/多栏识别。

    ocr 参数可注入替换默认实现，测试时不需要本机真的装好 Tesseract。

    只覆盖独立图片文件；"扫描件 PDF"（PDF 页面本身是图片、没有文字层）
    走 pdf_parser.py 里独立的渲染+OCR 逻辑（用 PyMuPDF 渲染页面，不需要
    poppler），parse_pdf() 接受同样的 OcrFunction，默认就是这里的
    _default_ocr。
    """
    ocr_fn = ocr or _default_ocr
    text = (await ocr_fn(path)).strip()
    if not text:
        return []
    return [Chunk(text=text, heading_path=[], source=str(path))]
