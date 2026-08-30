from __future__ import annotations

import json
import logging

from app.memory.llm_call import run_llm_text
from app.providers.base import ProviderRequest
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
    response_text = await run_llm_text(
        llm_registry=llm_registry,
        request=ProviderRequest(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ]
        ),
        provider_name=llm_provider_name,
        timeout_sec=timeout_sec,
        label="延迟意图检测",
        fallback_label="回退规则判断",
    )
    if response_text is None:
        return _looks_like_delay_by_rule(text)

    try:
        payload = json.loads(response_text)
        return bool(payload.get("is_delay", False))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return _looks_like_delay_by_rule(text)
