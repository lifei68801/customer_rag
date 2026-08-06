from __future__ import annotations

import asyncio
import json
import logging

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
    """
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
