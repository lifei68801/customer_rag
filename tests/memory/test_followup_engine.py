from datetime import datetime, timedelta

from app.memory.followup_engine import (
    FollowupTrigger,
    generate_followup_message,
    send_followup_if_allowed,
)
from app.memory.proactive_channel import MockProactiveChannel
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


class ExplodingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider 挂了")


def _llm_registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


def _profile(**overrides):
    base = {"is_vip": False, "feedback_label": "neutral", "communication_style": "formal"}
    base.update(overrides)
    return base


async def test_generate_followup_message_uses_llm_result():
    llm_registry = _llm_registry(ScriptedLLMProvider(["您好，关于您的问题我们想跟进一下。"]))
    trigger = FollowupTrigger(reason="ticket_pending_too_long", context="工单#123已挂起72小时")

    message = await generate_followup_message(
        trigger, profile=_profile(), llm_registry=llm_registry, llm_provider_name="llm"
    )

    assert message == "您好，关于您的问题我们想跟进一下。"


async def test_generate_followup_message_falls_back_to_template_on_llm_failure():
    llm_registry = _llm_registry(ExplodingLLMProvider())
    trigger = FollowupTrigger(reason="known_fix_available", context="故障已修复")

    message = await generate_followup_message(
        trigger, profile=_profile(), llm_registry=llm_registry, llm_provider_name="llm"
    )

    assert message  # 有内容，不是空字符串
    assert "故障已修复" in message or "已修复" in message


async def test_send_followup_if_allowed_sends_when_policy_permits():
    llm_registry = _llm_registry(ScriptedLLMProvider(["跟进消息内容"]))
    channel = MockProactiveChannel()
    trigger = FollowupTrigger(reason="ticket_pending_too_long", context="工单#123")

    result = await send_followup_if_allowed(
        trigger,
        tenant_id="t1",
        customer_id="c1",
        profile=_profile(),
        send_history=[],
        now=datetime(2026, 8, 5, 10, 0, 0),
        channel=channel,
        llm_registry=llm_registry,
        llm_provider_name="llm",
    )

    assert result.sent is True
    assert result.message == "跟进消息内容"
    assert channel.sent == [{"customer_id": "c1", "message": "跟进消息内容"}]


async def test_send_followup_if_allowed_skips_when_policy_denies():
    llm_registry = _llm_registry(ScriptedLLMProvider(["不应该被用到"]))
    channel = MockProactiveChannel()
    trigger = FollowupTrigger(reason="ticket_pending_too_long", context="工单#123")
    now = datetime(2026, 8, 5, 10, 0, 0)

    result = await send_followup_if_allowed(
        trigger,
        tenant_id="t1",
        customer_id="c1",
        profile=_profile(),
        send_history=[now - timedelta(seconds=10)],  # 刚发过，间隔太短
        now=now,
        channel=channel,
        llm_registry=llm_registry,
        llm_provider_name="llm",
    )

    assert result.sent is False
    assert result.message is None
    assert channel.sent == []
