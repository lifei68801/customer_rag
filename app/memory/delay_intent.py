from __future__ import annotations

import asyncio
import json
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是客服对话意图分类器。判断用户这句话是否表达了'稍后再自己尝试'、"
    "'之后需要跟进确认结果'的意图（例如'我先试试''稍后再试''待会弄'）。"
    '只输出 JSON：{"is_delay": true/false}。'
)

_DELAY_KEYWORDS = ("稍后再试", "待会试试", "过会儿再弄", "我先试试", "先试试")


def _looks_like_delay_by_rule(text: str) -> bool:
    return any(keyword in text for keyword in _DELAY_KEYWORDS)


async def detect_delay_intent(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> bool:
    """判断这句话是不是"稍后自己先试试、之后需要跟进确认"的意图；LLM
    失败/超时/解析失败时降级为关键词规则兜底。
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
        logger.info("延迟意图检测超时，回退规则判断")
        return _looks_like_delay_by_rule(text)
    except Exception:
        logger.warning("延迟意图检测失败，回退规则判断", exc_info=True)
        return _looks_like_delay_by_rule(text)

    try:
        payload = json.loads(result.text)
        return bool(payload.get("is_delay", False))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return _looks_like_delay_by_rule(text)
