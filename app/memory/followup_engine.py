from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.memory.delivery_policy import can_send_now, compute_delivery_policy
from app.memory.proactive_channel import ProactiveDeliveryChannel
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_REASON_TEMPLATES = {
    "ticket_pending_too_long": "您好，关于您反馈的问题（{context}），我们想跟进一下当前进展。",
    "known_fix_available": "您好，此前反馈的问题（{context}）现已修复，感谢您的耐心等待。",
    "delayed_confirmation": "您好，跟进一下之前的情况（{context}），想确认是否已经解决。",
}
_GENERIC_TEMPLATE = "您好，跟进一下：{context}"

_SYSTEM_PROMPT_TEMPLATE = (
    "你是客服主动跟进消息撰写助手。触发原因：{reason}。"
    "客户沟通风格偏好：{communication_style}（formal=正式，casual=亲切随和）。"
    "请写一句简短的主动跟进消息，语气符合客户偏好，直接给出消息文本，不要加任何解释。"
)


@dataclass(frozen=True)
class FollowupTrigger:
    reason: str  # "ticket_pending_too_long" | "known_fix_available" | "delayed_confirmation"
    context: str  # 人工可读的情境描述，如"工单#123已挂起72小时"


@dataclass(frozen=True)
class FollowupResult:
    sent: bool
    message: str | None
    skipped_reason: str | None = None


def _fallback_message(trigger: FollowupTrigger) -> str:
    template = _REASON_TEMPLATES.get(trigger.reason, _GENERIC_TEMPLATE)
    return template.format(context=trigger.context)


async def generate_followup_message(
    trigger: FollowupTrigger,
    *,
    profile: dict[str, Any],
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> str:
    """按客户画像的沟通风格生成跟进文案；LLM 失败/超时时降级为固定模板
    （按触发原因分类，不是一个万能模板），保证"发不出个性化文案"不等于
    "完全不跟进"。
    """
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {
                            "role": "system",
                            "content": _SYSTEM_PROMPT_TEMPLATE.format(
                                reason=trigger.reason,
                                communication_style=profile.get(
                                    "communication_style", "formal"
                                ),
                            ),
                        },
                        {"role": "user", "content": trigger.context},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("跟进文案生成超时，回退固定模板")
        return _fallback_message(trigger)
    except Exception:
        logger.warning("跟进文案生成失败，回退固定模板", exc_info=True)
        return _fallback_message(trigger)

    text = result.text.strip()
    return text or _fallback_message(trigger)


async def send_followup_if_allowed(
    trigger: FollowupTrigger,
    *,
    tenant_id: str,
    customer_id: str,
    profile: dict[str, Any],
    send_history: list[datetime],
    now: datetime,
    channel: ProactiveDeliveryChannel,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
) -> FollowupResult:
    """先过频率治理策略，允许才生成文案+真正发送；不允许时明确返回跳过
    原因，而不是静默什么都不做——调用方（比如批量扫描待跟进客户的脚本）
    需要知道"这条为什么没发"，用于监控/审计。
    """
    policy = compute_delivery_policy(profile)
    if not can_send_now(policy, send_history=send_history, now=now):
        return FollowupResult(sent=False, message=None, skipped_reason="rate_limited")

    message = await generate_followup_message(
        trigger,
        profile=profile,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
    )
    await channel.send(customer_id=customer_id, message=message)
    return FollowupResult(sent=True, message=message)
