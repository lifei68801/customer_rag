import asyncio

from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry
from app.qa.query_rewrite import rewrite_query


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_request: ProviderRequest | None = None

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.last_request = request
        return ProviderResult(text=self._text)


class FailingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        raise RuntimeError("provider unavailable")


class SlowLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        await asyncio.sleep(10)
        return ProviderResult(text="不应该被用到")


def _registry(provider) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", provider)
    return registry


async def test_uses_rewritten_query_when_llm_succeeds():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FixedLLMProvider("登录失败 认证模块 错误码")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录失败 认证模块 错误码"


async def test_falls_back_to_original_when_llm_raises():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录不了怎么办"


async def test_falls_back_to_original_when_llm_times_out():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(SlowLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=0.05,
    )

    assert result == "登录不了怎么办"


async def test_falls_back_to_original_when_llm_returns_empty():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FixedLLMProvider("   ")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录不了怎么办"


async def test_passes_conversation_context_to_the_rewrite_llm_call():
    # 客服口语化提问常有指代（"这个报错"），改写需要看到近期对话轮次
    # 才能补全指代，不能只看孤立的一句话。
    provider = FixedLLMProvider("网关超时错误码E502")
    conversation_context = [
        {"role": "user", "content": "我遇到了E502错误"},
        {"role": "assistant", "content": "E502是网关超时错误。"},
    ]

    result = await rewrite_query(
        "这个报错怎么解决",
        llm_registry=_registry(provider),
        llm_provider_name="llm",
        timeout_sec=1.0,
        conversation_context=conversation_context,
    )

    assert result == "网关超时错误码E502"
    assert provider.last_request is not None
    assert conversation_context[0] in provider.last_request.messages
    assert conversation_context[1] in provider.last_request.messages


async def test_conversation_context_is_optional():
    result = await rewrite_query(
        "登录不了怎么办",
        llm_registry=_registry(FixedLLMProvider("登录失败")),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result == "登录失败"


async def test_resolve_question_keeps_question_verbatim_when_not_depending_on_history():
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 3, "resolved_question": "Coca-Cola公司有多少个订单", '
        '"inherited_slots": [], "duplicate_of": ""}'
    )
    result = await resolve_question(
        "Coca-Cola公司有多少个订单",
        [{"role": "user", "content": "之前聊了别的"}],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
    )

    assert result.resolved_question == "Coca-Cola公司有多少个订单"
    assert result.inherited_slots == []
    assert result.duplicate_of is None


async def test_resolve_question_fills_anchor_slot_from_history():
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 1, "resolved_question": "Coca-Cola有多少个订单", '
        '"inherited_slots": ["anchor"], "duplicate_of": ""}'
    )
    result = await resolve_question(
        "它有多少个订单",
        [{"role": "user", "content": "Coca-Cola是什么公司"}],
        llm_registry=_registry(provider),
        llm_provider_name="llm",
    )

    assert result.resolved_question == "Coca-Cola有多少个订单"
    assert result.inherited_slots == ["anchor"]


async def test_resolve_question_drops_undefined_slot_names():
    # 槽位只有 anchor/intent_type/constraint 三个，模型幻觉出的其他名字
    # （比如照搬 swiftagent 的 time 槽位）必须被过滤掉，不能污染下游。
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 1, "resolved_question": "改写后的问题", '
        '"inherited_slots": ["anchor", "time", "dimension"], "duplicate_of": ""}'
    )
    result = await resolve_question(
        "原问题", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(provider), llm_provider_name="llm",
    )

    assert result.inherited_slots == ["anchor"]


async def test_resolve_question_reports_duplicate_question():
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 3, "resolved_question": "Coca-Cola公司有多少个订单", '
        '"inherited_slots": [], "duplicate_of": "Coca-Cola公司有多少个订单"}'
    )
    result = await resolve_question(
        "Coca-Cola公司有多少个订单",
        [{"role": "user", "content": "Coca-Cola公司有多少个订单"},
         {"role": "assistant", "content": "10000个"}],
        llm_registry=_registry(provider), llm_provider_name="llm",
    )

    assert result.duplicate_of == "Coca-Cola公司有多少个订单"


async def test_resolve_question_falls_back_to_original_on_llm_failure():
    from app.qa.query_rewrite import resolve_question

    result = await resolve_question(
        "它有多少个订单", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(FailingLLMProvider()), llm_provider_name="llm",
    )

    assert result.resolved_question == "它有多少个订单"
    assert result.inherited_slots == []
    assert result.duplicate_of is None


async def test_resolve_question_falls_back_to_original_on_timeout():
    from app.qa.query_rewrite import resolve_question

    result = await resolve_question(
        "它有多少个订单", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(SlowLLMProvider()), llm_provider_name="llm",
        timeout_sec=0.01,
    )

    assert result.resolved_question == "它有多少个订单"


async def test_resolve_question_falls_back_to_original_on_malformed_json():
    from app.qa.query_rewrite import resolve_question

    result = await resolve_question(
        "它有多少个订单", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(FixedLLMProvider("这不是JSON")),
        llm_provider_name="llm",
    )

    assert result.resolved_question == "它有多少个订单"
    assert result.inherited_slots == []
