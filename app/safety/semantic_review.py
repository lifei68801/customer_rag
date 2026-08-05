from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是客服回答的语义级安全审查员。"
    "判断这段回答是否包含规则匹配无法覆盖的问题：不当建议、误导性表述、"
    "违反平台规范的内容等。"
    '只输出 JSON：{"is_safe": true/false, "reason": "..."}'
)


@dataclass(frozen=True)
class SemanticSafetyResult:
    is_safe: bool
    reviewed: bool
    reason: str = ""


async def semantic_safety_review(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> SemanticSafetyResult:
    """LLM 语义级安全审查，作为规则级 check_text 之外的补充深度检查。

    失败/超时的安全默认值选择"放行但标记未审查"而非"判定不安全直接拦截"：
    规则级检查（check_text）已经是先行的一道安全网，语义审查是在其之上的
    增强而非唯一防线，把它当成硬性拦截点会导致 LLM 一抖动整个服务的输出
    就全部被挡，可用性代价太大；标记 reviewed=False 让调用方知道"这轮回答
    没有真正过语义审查"，可用于监控告警统计审查覆盖率。
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
        logger.info("语义安全审查超时，放行但标记未审查")
        return SemanticSafetyResult(is_safe=True, reviewed=False)
    except Exception:
        logger.warning("语义安全审查失败，放行但标记未审查", exc_info=True)
        return SemanticSafetyResult(is_safe=True, reviewed=False)

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        return SemanticSafetyResult(is_safe=True, reviewed=False)
    if not isinstance(payload, dict) or "is_safe" not in payload:
        return SemanticSafetyResult(is_safe=True, reviewed=False)

    return SemanticSafetyResult(
        is_safe=bool(payload.get("is_safe")),
        reviewed=True,
        reason=str(payload.get("reason", "")),
    )
