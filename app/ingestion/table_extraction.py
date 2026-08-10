from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

TableExtractionFunction = Callable[[Path], Awaitable[list[str]]]
"""输入一张页面截图路径，返回这一页表格里每条有效数据的语义完整陈述句
（比如"教育程度类别：博士，数量（人）：818"）——跟 OcrFunction
（app/ingestion/ocr_parser.py）返回一整段原始文字不同，这里返回的是已经
按数据点切好的列表，每个元素直接对应一个 Chunk。"""

_DEFAULT_MODEL = "qwen-vl-max"
"""不能随便挑一个"名字里带 vl"的模型——2026-08-10 实测过三个候选：
qwen-vl-ocr 是专用 OCR 模型，会完全无视下面这条结构化提取指令，只输出
它自己训练好的"文字块+坐标"格式；qwen-vl-plus 会遵循指令，但漏了近
一半页面内容（同一页测试只提取出下半部分的表格，上半部分的"股票期权
议案"记录整个丢失），还把"技术人员"识别错成"技术员"；只有 qwen-vl-max
和 qwen3-vl-plus 两次测试都完整准确。qwen-vl-max 对多分区表格的分区
上下文保留得更完整（比如"专业构成类别：生产人员"而不是裸的"生产人员"），
选它。换供应商/模型前必须用真实文档重新跑一遍这个验证，不能只看名字
判断能力，"贵的/新的模型更好"这个假设在 qwen-vl-plus 身上被证伪了。"""

_PROMPT = """请仔细阅读图片中的表格内容。这张表格可能包含多个逻辑分区（比如有分区标题行，
分区标题下面又有自己的子表头），也可能是逐行的"属性名:属性值"简介表，也可能是普通的单一表头表格。

请把表格里每一条有实际信息的数据，转换成一句完整、语义清晰、不丢失任何标签词和数值的陈述句
（格式类似"XX：YY"，XX是这条数据所属的类别/属性名，YY是对应的数值/内容），确保：
1. 不要用错误的表头去配对数据（比如不能把"教育程度"分区的表头安在"专业构成"分区的数据上）。
2. 每一行的所有关键信息（名称+数值）都要保留，不能只保留一半。
3. 表格外的页眉页脚、页码不需要提取。

以 JSON 数组格式输出，每个元素是一条字符串陈述句，不要输出任何解释性文字，只输出 JSON 数组本身。"""

_MIN_CALL_INTERVAL_SECONDS = 0.5
"""跟 dashscope_ocr.py 同款节流，防止连续高频调用触发服务端限流——这个
具体端点/模型还没有像 OCR 那样做过并发梯度实测，先保守沿用同样的间隔，
真实并发承受力需要单独实测（见 Settings.table_extraction_max_concurrency
的说明，默认并发数保守到 1）。"""


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "image/png"


def _parse_facts_json(raw_text: str) -> list[str]:
    """模型输出偶尔会用 ```json ... ``` 包裹（实测 qwen-vl-max 会这样，
    qwen3-vl-plus 不会）——先剥掉 markdown 代码块围栏再解析，不能假设
    模型总是老老实实只输出裸 JSON。
    """
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"表格提取模型返回的不是 JSON 数组: {raw_text[:200]!r}")
    return [str(item).strip() for item in data if str(item).strip()]


async def extract_table_facts(
    path: Path,
    *,
    base_url: str,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    client: httpx.AsyncClient | None = None,
    timeout: float = 90.0,
) -> list[str]:
    """用支持指令跟随的通用视觉大模型，从页面截图里直接语义提取表格数据，
    绕开 PyMuPDF find_tables() 提取表格结构再用规则猜表头这条路——后者
    对"多分区表格""逐行 key-value 简介表""双栏并排数据"这几类真实财报
    里常见的版面都处理得不好，2026-08-10 用真实文档验证过：视觉模型能
    正确处理这些规则处理不好的场景，包括正确拆分双栏并排的"董事会秘书/
    证券事务代表"两组不同人的信息，而不是像规则那样把两人的姓名混在
    一起。

    timeout 给到 90 秒（比 OCR 默认的 60 秒更长）：这是一次结构化推理+
    生成任务，不是单纯的文字识别，实测响应时间比纯 OCR 更长。
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
        raw_text = body["choices"][0]["message"]["content"]
        return _parse_facts_json(raw_text)
    finally:
        if owns_client:
            await resolved_client.aclose()


def build_table_extractor(
    *,
    base_url: str,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    client: httpx.AsyncClient | None = None,
) -> TableExtractionFunction:
    """固化 base_url/api_key/model，返回一个只需要页面截图路径就能调用的
    TableExtractionFunction。复用同一个 httpx.AsyncClient（连接复用）并
    在连续调用间强制最小间隔——跟 dashscope_ocr.py::build_dashscope_ocr
    是同一套模式，asyncio.Lock 只锁"等待+记录时间戳"，不锁实际网络请求，
    多个并发调用可以同时有请求在途。

    client 仅供测试注入 MockTransport；生产调用不传，用内部创建的真实
    httpx.AsyncClient。
    """
    resolved_client = client or httpx.AsyncClient(timeout=90.0)
    lock = asyncio.Lock()
    last_call_at: list[float] = [0.0]

    async def _throttled_extract(path: Path) -> list[str]:
        async with lock:
            wait = _MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - last_call_at[0])
            if wait > 0:
                await asyncio.sleep(wait)
            last_call_at[0] = time.monotonic()
        return await extract_table_facts(
            path, base_url=base_url, api_key=api_key, model=model, client=resolved_client
        )

    return _throttled_extract
