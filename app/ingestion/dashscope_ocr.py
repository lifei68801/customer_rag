from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import httpx

from app.ingestion.ocr_parser import OcrFunction

_DEFAULT_MODEL = "qwen-vl-ocr"
_PROMPT = "请提取图片中的所有文字，按原文顺序输出，不要做任何解释或总结。"
# 扫描件 PDF 逐页调用时两次请求之间的最小间隔——106 页顺序调用不加节流时
# 实测跑到 2 小时多还没完成 OCR 阶段（正常约 15-30 分钟），怀疑是短时间内
# 连续高频请求触发了服务端限流/排队，响应越来越慢但不报错，不会被现有的
# 异常处理捕获。加一个保守的最小间隔，用真实请求量换稳定的单次响应延迟。
_MIN_CALL_INTERVAL_SECONDS = 0.5


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "image/png"


async def dashscope_vision_ocr(
    path: Path,
    *,
    base_url: str,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    client: httpx.AsyncClient | None = None,
    timeout: float = 60.0,
) -> str:
    """用阿里百炼视觉OCR模型（走 OpenAI 兼容 chat completions 接口）识别
    图片里的文字，符合 ocr_parser.py 的 OcrFunction = Callable[[Path],
    Awaitable[str]] 接口，可以直接替换默认的 pytesseract 实现——不需要在
    本机安装任何 OCR 引擎二进制，只需要一个百炼 API key。

    异步调用（httpx.AsyncClient）：parse_pdf() 要对多个扫描页并发发起
    OCR 请求（见 pdf_parser.py 的 asyncio.Semaphore + asyncio.gather），
    同步阻塞调用没法在同一个事件循环里真正并发。
    """
    image_bytes = path.read_bytes()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    mime = _guess_mime_type(path)

    owns_client = client is None
    resolved_client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await resolved_client.post(
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
            await resolved_client.aclose()


def build_dashscope_ocr(
    *,
    base_url: str,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    client: httpx.AsyncClient | None = None,
) -> OcrFunction:
    """固化 base_url/api_key/model，返回一个只需要 path 就能调用的
    OcrFunction，可以直接传给 parse_pdf(ocr=...)/parse_image(ocr=...)。

    返回的函数复用同一个 httpx.AsyncClient（长连接/keep-alive，且原生支持
    多个协程共享同一个连接池并发发请求），并在连续调用间强制最小间隔——
    都是针对"扫描件 PDF 逐页调用"这个场景：不复用连接的话每页都要重新
    握手；不限速的话高频连续调用曾经实测导致响应越来越慢（不报错，只是
    慢，见 _MIN_CALL_INTERVAL_SECONDS 的说明）。asyncio.Lock 只锁"等待+
    记录时间戳"这一小段（不锁住实际的网络请求本身），所以多个并发调用
    可以同时有请求在途，只是发起时刻之间至少间隔 0.5 秒，避免瞬间打出
    一大批同时在途的请求。

    client 仅供测试注入 MockTransport；生产调用不传，用内部创建的真实
    httpx.AsyncClient。
    """
    resolved_client = client or httpx.AsyncClient(timeout=60.0)
    lock = asyncio.Lock()
    last_call_at: list[float] = [0.0]

    async def _throttled_ocr(path: Path) -> str:
        async with lock:
            wait = _MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - last_call_at[0])
            if wait > 0:
                await asyncio.sleep(wait)
            last_call_at[0] = time.monotonic()
        return await dashscope_vision_ocr(
            path, base_url=base_url, api_key=api_key, model=model, client=resolved_client
        )

    return _throttled_ocr
