from __future__ import annotations

import base64
import functools
from pathlib import Path

import httpx

from app.ingestion.ocr_parser import OcrFunction

_DEFAULT_MODEL = "qwen-vl-ocr"
_PROMPT = "请提取图片中的所有文字，按原文顺序输出，不要做任何解释或总结。"


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "image/png"


def dashscope_vision_ocr(
    path: Path,
    *,
    base_url: str,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> str:
    """用阿里百炼视觉OCR模型（走 OpenAI 兼容 chat completions 接口）识别
    图片里的文字，符合 ocr_parser.py 的 OcrFunction = Callable[[Path], str]
    接口，可以直接替换默认的 pytesseract 实现——不需要在本机安装任何
    OCR 引擎二进制，只需要一个百炼 API key。

    同步阻塞调用（httpx.Client 而非 AsyncClient）：parse_pdf/parse_image
    本身是同步函数，保持 OcrFunction 接口不变，不引入 async/sync 混用。
    """
    image_bytes = path.read_bytes()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    mime = _guess_mime_type(path)

    owns_client = client is None
    resolved_client = client or httpx.Client(timeout=timeout)
    try:
        response = resolved_client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{encoded}"},
                            },
                            {"type": "text", "text": _PROMPT},
                        ],
                    }
                ],
            },
        )
        response.raise_for_status()
        body = response.json()
        return body["choices"][0]["message"]["content"]
    finally:
        if owns_client:
            resolved_client.close()


def build_dashscope_ocr(
    *, base_url: str, api_key: str, model: str = _DEFAULT_MODEL
) -> OcrFunction:
    """固化 base_url/api_key/model，返回一个只需要 path 就能调用的
    OcrFunction，可以直接传给 parse_pdf(ocr=...)/parse_image(ocr=...)。
    """
    return functools.partial(
        dashscope_vision_ocr, base_url=base_url, api_key=api_key, model=model
    )
