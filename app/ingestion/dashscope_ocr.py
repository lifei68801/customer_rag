from __future__ import annotations

import base64
import threading
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
    *,
    base_url: str,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> OcrFunction:
    """固化 base_url/api_key/model，返回一个只需要 path 就能调用的
    OcrFunction，可以直接传给 parse_pdf(ocr=...)/parse_image(ocr=...)。

    返回的函数复用同一个 httpx.Client（长连接/keep-alive），并在连续调用间
    强制最小间隔——都是针对"扫描件 PDF 逐页调用"这个场景：不复用连接的话
    每页都要重新握手；不限速的话高频连续调用曾经实测导致响应越来越慢
    （不报错，只是慢，见 _MIN_CALL_INTERVAL_SECONDS 的说明）。lock 保证
    间隔计时在多线程下也生效——parse_pdf 目前是单线程调用，但
    ingestion_queue.py 是用 asyncio.to_thread 跑的，留了并发调用的余地。

    client 仅供测试注入 MockTransport；生产调用不传，用内部创建的真实
    httpx.Client。
    """
    resolved_client = client or httpx.Client(timeout=60.0)
    lock = threading.Lock()
    last_call_at: list[float] = [0.0]

    def _throttled_ocr(path: Path) -> str:
        with lock:
            wait = _MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - last_call_at[0])
            if wait > 0:
                time.sleep(wait)
            last_call_at[0] = time.monotonic()
        return dashscope_vision_ocr(
            path, base_url=base_url, api_key=api_key, model=model, client=resolved_client
        )

    return _throttled_ocr
