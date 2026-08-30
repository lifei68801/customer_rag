"""带超时和兜底的 LLM 文本调用。

app/memory 下有六个模块要向 LLM 问一个小问题（抽取事实、判定记忆冲突、
识别延迟意图、解析时间表达式、识别纠错意图、生成跟进文案），六处各自
重复了同一段控制流：asyncio.wait_for 包住 registry.run，超时按 INFO 记、
其它异常按 WARNING 带堆栈记，两条路径都退回各自的规则兜底。

差异只有两处：日志里的业务标签，和兜底表达式本身。兜底留在调用方——它是
每个模块自己的策略（有的回退空列表，有的回退关键词规则，有的回退固定
模板），不该被抽走；这里只收敛"怎么调、超时算什么、失败记什么级别"。

解析不在这里：六处的 JSON/布尔解析本来就写在各自独立的 try 里，有各自的
失败文案和返回形状，合并它们只会把两个不相干的失败模式混成一个。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from app.providers.base import ProviderCapability, ProviderRequest

logger = logging.getLogger(__name__)


class _LLMRegistry(Protocol):
    """这六个调用点用到的唯一方法——不引 ProviderRegistry 具体类型，测试
    可以用一个只实现 run() 的替身。"""

    async def run(self, capability, request, *, provider_name): ...


async def run_llm_text(
    *,
    llm_registry: _LLMRegistry,
    request: ProviderRequest,
    provider_name: str,
    timeout_sec: float,
    label: str,
    fallback_label: str,
) -> str | None:
    """发起一次 LLM 调用并返回文本；超时或失败时返回 None（已经记过日志）。

    调用方拿到 None 就走自己的兜底，不需要再记一遍日志。label/fallback_label
    只进日志，拼成合并前逐字相同的那两句话（"X超时，Y" / "X失败，Y"）——
    这些是运维在看的字符串，收敛实现不该顺带改掉它们。

    超时记 INFO 不记 WARNING：它是预期内的降级路径，每个调用点都配了兜底。
    其它异常记 WARNING 且带 exc_info——那类失败是意外的，没有堆栈无从排查。

    asyncio.CancelledError 不在捕获范围内（它继承 BaseException，不是
    Exception），会原样上抛：上游取消时不该被当成"调用失败"吞掉，让调用方
    白算一遍兜底。
    """
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM, request, provider_name=provider_name
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("%s超时，%s", label, fallback_label)
        return None
    except Exception:
        logger.warning("%s失败，%s", label, fallback_label, exc_info=True)
        return None
    return result.text
