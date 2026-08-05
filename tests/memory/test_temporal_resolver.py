from datetime import datetime

from app.memory.temporal_resolver import resolve_time_window
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


async def test_resolve_time_window_uses_llm_result_when_valid():
    llm_registry = _llm_registry(
        ScriptedLLMProvider(
            [
                '{"start": "2026-07-27T00:00:00", "end": "2026-07-28T00:00:00", "confidence": 0.9}'
            ]
        )
    )
    reference_time = datetime(2026, 8, 5, 10, 0, 0)

    result = await resolve_time_window(
        "上周五",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        reference_time=reference_time,
    )

    assert result.resolved is True
    assert result.start == datetime(2026, 7, 27, 0, 0, 0)
    assert result.end == datetime(2026, 7, 28, 0, 0, 0)
    assert result.confidence == 0.9
    assert result.is_future is False


async def test_resolve_time_window_falls_back_to_rules_on_llm_failure():
    llm_registry = _llm_registry(ExplodingLLMProvider())
    reference_time = datetime(2026, 8, 5, 10, 0, 0)

    result = await resolve_time_window(
        "昨天",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        reference_time=reference_time,
    )

    assert result.resolved is True
    assert result.start == datetime(2026, 8, 4, 0, 0, 0)
    assert result.end == datetime(2026, 8, 5, 0, 0, 0)


async def test_resolve_time_window_falls_back_to_rules_on_low_confidence():
    llm_registry = _llm_registry(
        ScriptedLLMProvider(
            [
                '{"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00", "confidence": 0.2}'
            ]
        )
    )
    reference_time = datetime(2026, 8, 5, 10, 0, 0)

    result = await resolve_time_window(
        "今天",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        reference_time=reference_time,
        min_confidence=0.5,
    )

    assert result.resolved is True
    assert result.start == datetime(2026, 8, 5, 0, 0, 0)
    assert result.end == datetime(2026, 8, 6, 0, 0, 0)


async def test_resolve_time_window_returns_unresolved_when_no_rule_matches_and_llm_fails():
    llm_registry = _llm_registry(ExplodingLLMProvider())
    reference_time = datetime(2026, 8, 5, 10, 0, 0)

    result = await resolve_time_window(
        "完全无关的问题",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        reference_time=reference_time,
    )

    assert result.resolved is False
    assert result.start is None
    assert result.end is None


async def test_resolve_time_window_flags_future_time():
    llm_registry = _llm_registry(
        ScriptedLLMProvider(
            [
                '{"start": "2026-12-01T00:00:00", "end": "2026-12-02T00:00:00", "confidence": 0.9}'
            ]
        )
    )
    reference_time = datetime(2026, 8, 5, 10, 0, 0)

    result = await resolve_time_window(
        "12月1日",
        llm_registry=llm_registry,
        llm_provider_name="llm",
        reference_time=reference_time,
    )

    assert result.resolved is True
    assert result.is_future is True
