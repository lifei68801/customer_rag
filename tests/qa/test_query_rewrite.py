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


async def test_resolve_question_falls_back_when_provider_returns_none_text():
    # provider 违反 ProviderResult.text 的 str 类型契约、给出 None 时，
    # json.loads(None) 抛的是 TypeError 而不是 JSONDecodeError。这个函数
    # 没有外层 try/except（graph.py::resolve_question_node 直接 await 它），
    # 漏接会把整轮对话打挂，而不只是这一步消解失败。
    from app.qa.query_rewrite import resolve_question

    result = await resolve_question(
        "它有多少个订单", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(FixedLLMProvider(None)),
        llm_provider_name="llm",
    )

    assert result.resolved_question == "它有多少个订单"
    assert result.inherited_slots == []
    assert result.duplicate_of is None


async def test_resolve_question_falls_back_when_json_is_not_an_object():
    # 合法 JSON 但不是对象（数组/标量）——模型偶尔会直接吐一个列表或数字。
    # payload.get(...) 在这种值上会 AttributeError，所以必须在解析后、取字段前
    # 就挡住，不能只靠 json.JSONDecodeError。
    from app.qa.query_rewrite import resolve_question

    for malformed in ("[1, 2, 3]", '"just a string"', "42"):
        result = await resolve_question(
            "它有多少个订单", [{"role": "user", "content": "历史"}],
            llm_registry=_registry(FixedLLMProvider(malformed)),
            llm_provider_name="llm",
        )

        assert result.resolved_question == "它有多少个订单"
        assert result.inherited_slots == []
        assert result.duplicate_of is None


async def test_resolve_question_ignores_non_list_inherited_slots():
    # inherited_slots 该是数组，模型可能给成对象。这里必须用 dict 而不是
    # 字符串来构造：字符串 "anchor" 迭代出来是单个字符，每个字符都不在
    # _SLOT_NAMES 里，去掉 isinstance 守卫后结果同样是 []，那样的测试证明
    # 不了守卫的存在。dict 迭代出来是键名 "anchor"，它确实在白名单里——
    # 守卫在场得 []，守卫缺席得 ["anchor"]，这才有区分度。
    from app.qa.query_rewrite import resolve_question

    provider = FixedLLMProvider(
        '{"rl": 1, "resolved_question": "改写后的问题", '
        '"inherited_slots": {"anchor": true}, "duplicate_of": ""}'
    )
    result = await resolve_question(
        "原问题", [{"role": "user", "content": "历史"}],
        llm_registry=_registry(provider), llm_provider_name="llm",
    )

    assert result.inherited_slots == []


async def test_rewrite_query_system_prompt_no_longer_mentions_history():
    """Layer 1 统一接管了指代消解，rewrite_query 的职责收窄成只做检索友好化，
    提示词里不该再让它自己去"结合对话历史补全指代"——那会变成两处各自
    独立做同一件事，正是这次重构要消除的模式。"""
    from app.qa.query_rewrite import _SYSTEM_PROMPT

    assert "对话历史" not in _SYSTEM_PROMPT
    assert "原样返回" in _SYSTEM_PROMPT
