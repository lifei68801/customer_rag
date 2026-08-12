from __future__ import annotations

import asyncio
import json
import logging
import re

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是客服对话意图分类器。判断用户这句话是否在纠正你之前记错的信息"
    '（例如"你记错了""其实不是这样""应该是...不是..."）。'
    '只输出 JSON：{"is_correction": true/false}。'
)

_CORRECTION_KEYWORDS = ("记错了", "弄错了", "不对，应该是", "更正一下", "搞错了")


def _looks_like_correction_by_rule(text: str) -> bool:
    return any(keyword in text for keyword in _CORRECTION_KEYWORDS)


# detect_correction_intent 之前对每条消息都无条件触发一次 LLM 调用，2026-08-12
# 排查"响应到第一个字之前等太久"时发现这是链路里又一处"能跳过就跳过"——
# 绝大多数问题根本不是在纠正什么。这里的正则和 graph.py 里的
# _TEMPORAL_CUE_PATTERN 同一个设计原则：故意放宽（宁可漏判"跳过"、不能
# 漏判"应该走 LLM"），只有完全匹配不到任何纠正类线索时才跳过 LLM 调用、
# 直接判 False；命中时仍然老老实实走完整的 LLM 语义判断（正则本身不够
# 精确到能替代 LLM 做最终判断，例如"不是...是..."这类结构在正常陈述句里
# 也很常见，只用来决定"值不值得为这句话多打一次 LLM 请求"）。
_CORRECTION_CUE_PATTERN = re.compile(
    r"记错|弄错|搞错|说错|写错|听错|看错|理解错|记混|搞混|"
    r"不对[，,。！]|应该是|其实是|其实应该|更正|纠正|改成|改为|"
    r"不是.{0,15}是"
)


def _looks_like_possible_correction(text: str) -> bool:
    return bool(_CORRECTION_CUE_PATTERN.search(text))


async def detect_correction_intent(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> bool:
    """判断这句话是不是在纠正之前记错的信息；LLM 失败/超时/解析失败时
    降级为关键词规则兜底，规则命中即判 True——宁可多触发一次短路由走
    完整决策链路确认，也不要漏判导致纠正没生效。

    正式调用 LLM 前先过一道 _looks_like_possible_correction 规则前置过滤：
    不匹配时直接返回 False，跳过这次 LLM 往返，省掉客服问答链路里绝大多数
    "显然不是纠正"消息的一次请求耗时。
    """
    if not _looks_like_possible_correction(text):
        return False
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("纠错意图检测超时，回退规则判断")
        return _looks_like_correction_by_rule(text)
    except Exception:
        logger.warning("纠错意图检测失败，回退规则判断", exc_info=True)
        return _looks_like_correction_by_rule(text)

    try:
        payload = json.loads(result.text)
        return bool(payload.get("is_correction", False))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return _looks_like_correction_by_rule(text)
