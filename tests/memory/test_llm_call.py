import asyncio
import logging

import pytest

from app.memory.llm_call import run_llm_text
from app.providers.base import ProviderRequest

pytestmark = pytest.mark.anyio


class _StubRegistry:
    """只实现 run()——这是 6 个调用点唯一用到的方法。"""

    def __init__(self, *, text: str | None = None, exc: Exception | None = None, delay: float = 0.0):
        self._text = text
        self._exc = exc
        self._delay = delay
        self.calls: list[tuple] = []

    async def run(self, capability, request, *, provider_name):
        self.calls.append((capability, request, provider_name))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc

        class _Result:
            text = self._text

        return _Result()


def _request() -> ProviderRequest:
    return ProviderRequest(messages=[{"role": "user", "content": "hi"}])


async def test_returns_text_on_success():
    registry = _StubRegistry(text='{"facts": []}')

    text = await run_llm_text(
        llm_registry=registry, request=_request(), provider_name="p",
        timeout_sec=1.0, label="事实抽取", fallback_label="回退空列表",
    )

    assert text == '{"facts": []}'
    assert len(registry.calls) == 1
    assert registry.calls[0][2] == "p"


async def test_timeout_returns_none_and_logs_info(caplog):
    """超时按 INFO 记，不是 WARNING。

    超时是这些调用预期内的降级路径（每个调用点都配了规则兜底），用 WARNING
    会让正常运行的日志里充满噪音。合并前 6 份副本一致地用 info，这里钉住。
    """
    registry = _StubRegistry(text="never", delay=0.2)

    with caplog.at_level(logging.INFO):
        text = await run_llm_text(
            llm_registry=registry, request=_request(), provider_name="p",
            timeout_sec=0.01, label="事实抽取", fallback_label="回退空列表",
        )

    assert text is None
    record = next(r for r in caplog.records if "事实抽取" in r.message)
    assert record.levelno == logging.INFO
    assert "超时" in record.message
    assert "回退空列表" in record.message


async def test_failure_returns_none_and_logs_warning_with_traceback(caplog):
    """非超时异常按 WARNING 记，并且必须带 exc_info。

    这类失败是意外的（provider 配错、鉴权失败、返回体畸形），没有堆栈就只
    剩一句"失败"，无从排查。合并前 6 份副本都传了 exc_info=True。
    """
    registry = _StubRegistry(exc=RuntimeError("provider 炸了"))

    with caplog.at_level(logging.INFO):
        text = await run_llm_text(
            llm_registry=registry, request=_request(), provider_name="p",
            timeout_sec=1.0, label="记忆冲突决策", fallback_label="降级规则模式",
        )

    assert text is None
    record = next(r for r in caplog.records if "记忆冲突决策" in r.message)
    assert record.levelno == logging.WARNING
    assert "失败" in record.message
    assert "降级规则模式" in record.message
    assert record.exc_info is not None


async def test_does_not_swallow_cancellation():
    """CancelledError 必须原样往上抛，不能被当成"调用失败"吞掉。

    这些调用跑在请求处理链路里，上游取消（客户端断开、整轮超时）时如果被
    这里吞成 None，调用方会继续执行兜底逻辑、把一个已经没人要的结果算完。
    Python 3.8+ 的 CancelledError 继承 BaseException 而不是 Exception，
    `except Exception` 天然不会捕获它——这条用例把这个依赖固定下来，避免
    以后有人改成 `except BaseException` 或加宽捕获范围。
    """
    registry = _StubRegistry(exc=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await run_llm_text(
            llm_registry=registry, request=_request(), provider_name="p",
            timeout_sec=1.0, label="x", fallback_label="y",
        )
