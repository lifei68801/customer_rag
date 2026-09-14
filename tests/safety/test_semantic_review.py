from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry
from app.safety.semantic_review import semantic_safety_review


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


async def test_llm_flags_unsafe_content():
    result = await semantic_safety_review(
        "这是一段包含违规建议的内容",
        llm_registry=_registry(
            FixedLLMProvider('{"is_safe": false, "reason": "包含违规建议"}')
        ),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result.is_safe is False
    assert result.reviewed is True


async def test_llm_confirms_safe_content():
    result = await semantic_safety_review(
        "重启路由器即可解决网络问题",
        llm_registry=_registry(FixedLLMProvider('{"is_safe": true, "reason": ""}')),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result.is_safe is True
    assert result.reviewed is True


async def test_llm_failure_falls_back_to_unreviewed_but_not_blocked():
    result = await semantic_safety_review(
        "重启路由器即可解决网络问题",
        llm_registry=_registry(FailingLLMProvider()),
        llm_provider_name="llm",
        timeout_sec=1.0,
    )

    assert result.is_safe is True
    assert result.reviewed is False


def test_system_prompt_mentions_internal_data_leakage():
    from app.safety.semantic_review import _SYSTEM_PROMPT

    assert "内部数据" in _SYSTEM_PROMPT or "内部信息" in _SYSTEM_PROMPT


def test_system_prompt_says_queried_business_data_is_not_a_leak():
    """审查员要知道回答里的事实来自租户自己的知识库、提问者已登录。

    不说的话它按"公开渠道的客服回复"去判：demo 租户问 Coca-Cola 下的订单号，
    回答列出 10 个订单号，审查员 4 次里 4 次判不安全，理由是"未脱敏的订单
    数据"——而那正是用户要的答案。这条测试钉的是提示词里的约定本身；审查员
    实际怎么判，只有真实 LLM 回答得了（本次改动用 9 类样例各跑 3 次验证过）。
    """
    from app.safety.semantic_review import _SYSTEM_PROMPT

    assert "自己的知识库" in _SYSTEM_PROMPT
    assert "已登录" in _SYSTEM_PROMPT
    assert "订单号" in _SYSTEM_PROMPT and "不算泄露" in _SYSTEM_PROMPT


def test_system_prompt_still_blocks_personal_data_credentials_and_internals():
    """放宽的只是"业务数据本身"。个人隐私、凭据、系统内部信息必须仍在拦截清单里。"""
    from app.safety.semantic_review import _SYSTEM_PROMPT

    for must_block in ("手机号", "身份证号", "住址", "密码", "密钥", "系统提示词", "node_key", "堆栈"):
        assert must_block in _SYSTEM_PROMPT, must_block
