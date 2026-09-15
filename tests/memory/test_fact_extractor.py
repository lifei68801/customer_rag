from app.memory.fact_extractor import extract_facts
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


async def test_extracts_facts_from_valid_json_response():
    facts = await extract_facts(
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
        llm_registry=_registry(FixedLLMProvider('{"facts": ["客户使用企业版套餐"]}')),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert facts == ["客户使用企业版套餐"]


async def test_falls_back_to_empty_list_when_llm_fails():
    facts = await extract_facts(
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert facts == []


# ── 数字来源守卫 ────────────────────────────────────────────────────────
#
# 真实污染：demo 租户 76 条长期记忆里 37 条是知识库查询结果，其中 15 条是
# 同一句"Coca-Cola 公司共有 10,000 个订单"的不同措辞。那个 10000 是当时图谱
# 的中间结果，数据改一次就错——而它会在上下文里盖过工具的实时返回（实测：
# 工具返回 matched_count=0，模型仍然答出"匹配到 10000 条"）。


def test_a_count_that_only_the_assistant_mentioned_is_dropped():
    from app.memory.fact_extractor import drop_facts_whose_numbers_came_only_from_the_assistant

    kept = drop_facts_whose_numbers_came_only_from_the_assistant(
        ["Coca-Cola公司共有10,000个订单"],
        user_input="Coca-Cola 公司有多少个订单",
        assistant_output="按图谱统计，共有 10000 个订单。",
    )

    assert kept == []


def test_numbers_the_user_said_are_kept():
    """用户自己说的数字是真实的长期约束，不能连坐丢掉。"""
    from app.memory.fact_extractor import drop_facts_whose_numbers_came_only_from_the_assistant

    kept = drop_facts_whose_numbers_came_only_from_the_assistant(
        ["用户需要 20 个席位"],
        user_input="我们要 20 个席位",
        assistant_output="好的，已记录 20 个席位。",
    )

    assert kept == ["用户需要 20 个席位"]


def test_facts_without_numbers_are_untouched():
    from app.memory.fact_extractor import drop_facts_whose_numbers_came_only_from_the_assistant

    kept = drop_facts_whose_numbers_came_only_from_the_assistant(
        ["用户偏好邮件通知"], user_input="以后用邮件通知我", assistant_output="已记录。",
    )

    assert kept == ["用户偏好邮件通知"]


def test_thousands_separators_do_not_fool_the_guard():
    """记忆里写 "10,000"、助手回答里写 "10000"——不归一化就对不上，守卫失效。"""
    from app.memory.fact_extractor import drop_facts_whose_numbers_came_only_from_the_assistant

    assert drop_facts_whose_numbers_came_only_from_the_assistant(
        ["共有 10，000 个订单"], user_input="有多少订单", assistant_output="10000 个",
    ) == []


def test_the_prompt_says_query_results_are_not_facts():
    """守卫只挡住带数字的那一类。"Coca-Cola 关联了 Cola、Pepsi 两个产品"这种
    没有数字的查询结果，只能靠提示词。"""
    from app.memory.fact_extractor import _SYSTEM_PROMPT

    assert "知识库" in _SYSTEM_PROMPT and "不要抽取" in _SYSTEM_PROMPT


async def test_extract_facts_applies_the_guard_even_when_the_model_ignores_the_prompt():
    """提示词已经说了"不要抽知识库查询结果"，但模型不一定听——这次污染正是它
    没听。守卫必须接在 extract_facts 出口上，不能只作为一个可选的工具函数存在。"""
    facts = await extract_facts(
        user_input="Coca-Cola 公司有多少个订单",
        assistant_output="按图谱统计，Coca-Cola 公司共有 10000 个订单。",
        llm_registry=_registry(
            FixedLLMProvider('{"facts": ["Coca-Cola公司共有10,000个订单", "用户关注 Coca-Cola 的订单规模"]}')
        ),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert facts == ["用户关注 Coca-Cola 的订单规模"]
