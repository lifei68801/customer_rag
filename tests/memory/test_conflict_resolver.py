from app.memory.conflict_resolver import resolve_memory_actions
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider unavailable")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_llm_decides_update_action_with_target_memory_id():
    llm_text = (
        '{"actions": [{"event": "UPDATE", "target_memory_id": "m1", '
        '"text": "客户已升级为旗舰版套餐", "reason": "套餐变更"}]}'
    )
    actions = await resolve_memory_actions(
        new_facts=["客户已升级为旗舰版套餐"],
        existing_memories=[{"memory_id": "m1", "text": "客户使用企业版套餐"}],
        llm_registry=_registry(FixedLLMProvider(llm_text)),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert actions == [
        {
            "event": "UPDATE",
            "memory_id": "m1",
            "text": "客户已升级为旗舰版套餐",
            "reason": "套餐变更",
        }
    ]


async def test_falls_back_to_add_for_new_fact_when_llm_fails():
    actions = await resolve_memory_actions(
        new_facts=["客户使用企业版套餐"],
        existing_memories=[],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert actions == [
        {"event": "ADD", "memory_id": "", "text": "客户使用企业版套餐", "reason": "fallback"}
    ]


async def test_falls_back_to_none_for_duplicate_fact_when_llm_fails():
    actions = await resolve_memory_actions(
        new_facts=["客户使用企业版套餐"],
        existing_memories=[{"memory_id": "m1", "text": "客户使用企业版套餐"}],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert actions == [
        {"event": "NONE", "memory_id": "", "text": "客户使用企业版套餐", "reason": "fallback"}
    ]
