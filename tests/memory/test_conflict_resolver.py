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
            "conflict_type": "",
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
        {
            "event": "ADD", "memory_id": "", "text": "客户使用企业版套餐",
            "reason": "fallback", "conflict_type": "",
        }
    ]


async def test_falls_back_to_none_for_duplicate_fact_when_llm_fails():
    # 注：新事实与已有记忆文本完全一致，现在会被精确去重短路直接拦截，
    # 根本不会走到 LLM 调用/fallback 这一步，所以 reason 从 "fallback"
    # 变为 "精确文本重复"。此测试与新增的
    # test_exact_text_duplicate_short_circuits_without_calling_llm 输入完全
    # 相同，保留是为了不删旧测试；行为断言以短路路径为准。
    actions = await resolve_memory_actions(
        new_facts=["客户使用企业版套餐"],
        existing_memories=[{"memory_id": "m1", "text": "客户使用企业版套餐"}],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert actions == [
        {
            "event": "NONE", "memory_id": "", "text": "客户使用企业版套餐",
            "reason": "精确文本重复", "conflict_type": "",
        }
    ]


async def test_llm_decides_update_with_conflict_type():
    llm_text = (
        '{"actions": [{"event": "UPDATE", "target_memory_id": "m1", '
        '"text": "客户已升级为旗舰版套餐", "reason": "套餐变更", "conflict_type": "value"}]}'
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
            "event": "UPDATE", "memory_id": "m1", "text": "客户已升级为旗舰版套餐",
            "reason": "套餐变更", "conflict_type": "value",
        }
    ]


async def test_illegal_conflict_type_normalizes_to_empty_string():
    llm_text = (
        '{"actions": [{"event": "UPDATE", "target_memory_id": "m1", '
        '"text": "新内容", "reason": "r", "conflict_type": "relationship"}]}'
    )
    actions = await resolve_memory_actions(
        new_facts=["新内容"],
        existing_memories=[{"memory_id": "m1", "text": "旧内容"}],
        llm_registry=_registry(FixedLLMProvider(llm_text)),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert actions[0]["conflict_type"] == ""
    assert actions[0]["event"] == "UPDATE"  # 分类非法不影响决策本身被采纳


async def test_add_event_conflict_type_can_be_empty():
    llm_text = '{"actions": [{"event": "ADD", "text": "新事实", "reason": "r"}]}'
    actions = await resolve_memory_actions(
        new_facts=["新事实"], existing_memories=[],
        llm_registry=_registry(FixedLLMProvider(llm_text)),
        llm_provider_name="llm", timeout_sec=1.0,
    )

    assert actions[0]["conflict_type"] == ""


async def test_fallback_actions_include_empty_conflict_type():
    actions = await resolve_memory_actions(
        new_facts=["客户使用企业版套餐"], existing_memories=[],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm", timeout_sec=1.0,
    )

    assert actions == [
        {
            "event": "ADD", "memory_id": "", "text": "客户使用企业版套餐",
            "reason": "fallback", "conflict_type": "",
        }
    ]


async def test_add_event_conflict_type_from_llm_is_forced_to_empty():
    """LLM 不该给 ADD 事件带 conflict_type（系统提示词明确说了不需要），但
    万一它无视提示词硬塞了一个合法值，也要被强制清空——conflict_type 只对
    UPDATE/DELETE 有意义，让它漏到 ADD/NONE 行会污染 memory_history 的审计
    列（离线按 conflict_type 分组统计冲突数的查询会虚高）。"""
    llm_text = (
        '{"actions": [{"event": "ADD", "text": "新事实", "reason": "r", '
        '"conflict_type": "value"}]}'
    )
    actions = await resolve_memory_actions(
        new_facts=["新事实"], existing_memories=[],
        llm_registry=_registry(FixedLLMProvider(llm_text)),
        llm_provider_name="llm", timeout_sec=1.0,
    )

    assert actions[0]["event"] == "ADD"
    assert actions[0]["conflict_type"] == ""


async def test_exact_text_duplicate_short_circuits_without_calling_llm():
    """新事实文本跟已有记忆完全一致时直接判 NONE，不发起 LLM 调用——用一个
    会抛异常的 provider 验证"根本没被调用"，而不是"调用失败后降级"，
    两者产出的 reason 不同（fallback vs 精确文本重复），断言 reason 能
    区分这两条路径。"""
    actions = await resolve_memory_actions(
        new_facts=["客户使用企业版套餐"],
        existing_memories=[{"memory_id": "m1", "text": "客户使用企业版套餐"}],
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm", timeout_sec=1.0,
    )

    assert actions == [
        {
            "event": "NONE", "memory_id": "", "text": "客户使用企业版套餐",
            "reason": "精确文本重复", "conflict_type": "",
        }
    ]


async def test_exact_text_duplicate_mixed_with_new_fact_only_calls_llm_for_new_fact():
    llm_text = '{"actions": [{"event": "ADD", "text": "新事实B", "reason": "r"}]}'
    actions = await resolve_memory_actions(
        new_facts=["已有事实A", "新事实B"],
        existing_memories=[{"memory_id": "m1", "text": "已有事实A"}],
        llm_registry=_registry(FixedLLMProvider(llm_text)),
        llm_provider_name="llm", timeout_sec=1.0,
    )

    assert len(actions) == 2
    short_circuited = next(a for a in actions if a["text"] == "已有事实A")
    from_llm = next(a for a in actions if a["text"] == "新事实B")
    assert short_circuited == {
        "event": "NONE", "memory_id": "", "text": "已有事实A",
        "reason": "精确文本重复", "conflict_type": "",
    }
    assert from_llm["event"] == "ADD"


async def test_all_facts_exact_duplicates_returns_without_llm_call():
    actions = await resolve_memory_actions(
        new_facts=["已有事实A"],
        existing_memories=[{"memory_id": "m1", "text": "已有事实A"}],
        llm_registry=_registry(FailingLLMProvider()),  # 会抛异常，证明没被调用
        llm_provider_name="llm", timeout_sec=1.0,
    )

    assert len(actions) == 1
    assert actions[0]["event"] == "NONE"
