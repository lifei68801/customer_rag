from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = (
    "你是时间表达式解析器。当前时间是 {reference_time}（ISO格式）。"
    "把用户提到的相对时间表达式解析为具体的起止时间窗口。"
    '只输出 JSON：{{"start":"ISO8601","end":"ISO8601","confidence":0-1}}。'
    "无法判断时 confidence 给低分，不要编造一个自己不确定的具体时间。"
)


@dataclass(frozen=True)
class TimeWindowResult:
    resolved: bool
    start: datetime | None
    end: datetime | None
    confidence: float
    is_future: bool
    source: str  # "llm" | "rule" | "unresolved"


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


# 规则引擎兜底：只覆盖客服场景里最常见的几种相对时间表达，不追求覆盖
# 所有自然语言时间表述——LLM 解析失败/低置信度时，这几条比"完全不解析"
# 更有用，但不试图取代一个真正的时间表达式解析库。
_RULE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile("今天"), 0),
    (re.compile("昨天"), -1),
    (re.compile("前天"), -2),
    (re.compile("明天"), 1),
]


def _resolve_by_rule(text: str, *, reference_time: datetime) -> TimeWindowResult | None:
    for pattern, day_offset in _RULE_PATTERNS:
        if pattern.search(text):
            target_day = reference_time + timedelta(days=day_offset)
            start, end = _day_bounds(target_day)
            return TimeWindowResult(
                resolved=True,
                start=start,
                end=end,
                confidence=1.0,
                is_future=start > reference_time,
                source="rule",
            )
    return None


async def resolve_time_window(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    reference_time: datetime,
    min_confidence: float = 0.5,
    timeout_sec: float = 2.0,
) -> TimeWindowResult:
    """把自然语言时间表达解析为具体的起止时间窗口。

    优先 LLM 解析（能处理"上周五""三周前"这类规则引擎覆盖不到的表达），
    LLM 失败/超时/JSON 解析失败/置信度低于 min_confidence 时降级到规则
    引擎（只覆盖"今天/昨天/前天/明天"这几个最常见、最不该出错的场景）；
    规则也没匹配上则返回 resolved=False，不编造一个时间窗口。

    is_future 标记解析出的窗口是否晚于 reference_time——由调用方决定
    "识别到未来时间"之后要不要转向用户澄清（架构文档 §6.4 的"未来时间
    窗口保护"），这里只负责标记事实，不做路由决策。
    """
    llm_result = await _resolve_by_llm(
        text,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        reference_time=reference_time,
        timeout_sec=timeout_sec,
    )
    if llm_result is not None and llm_result.confidence >= min_confidence:
        return llm_result

    rule_result = _resolve_by_rule(text, reference_time=reference_time)
    if rule_result is not None:
        return rule_result

    return TimeWindowResult(
        resolved=False,
        start=None,
        end=None,
        confidence=0.0,
        is_future=False,
        source="unresolved",
    )


async def _resolve_by_llm(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    reference_time: datetime,
    timeout_sec: float,
) -> TimeWindowResult | None:
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {
                            "role": "system",
                            "content": _SYSTEM_PROMPT_TEMPLATE.format(
                                reference_time=reference_time.isoformat()
                            ),
                        },
                        {"role": "user", "content": text},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("时间表达式解析超时，回退规则引擎")
        return None
    except Exception:
        logger.warning("时间表达式解析失败，回退规则引擎", exc_info=True)
        return None

    try:
        payload = json.loads(result.text)
        # reference_time 是 naive 本地时间（无 tz 后缀）传给 LLM 的，但 LLM 有时会自行
        # 在返回的 ISO8601 里加上 Z/+00:00 之类的时区后缀——这个后缀没有实际依据（LLM并不
        # 知道本地时区是什么），直接丢弃 tzinfo 而不做时区换算，保持和 reference_time
        # 同一个 naive 本地时间语义，否则会在下面和 reference_time 比较时抛
        # TypeError: can't compare offset-naive and offset-aware datetimes。
        start = datetime.fromisoformat(payload["start"]).replace(tzinfo=None)
        end = datetime.fromisoformat(payload["end"]).replace(tzinfo=None)
        confidence = float(payload["confidence"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        logger.warning("时间表达式解析返回格式不合法，回退规则引擎")
        return None

    return TimeWindowResult(
        resolved=True,
        start=start,
        end=end,
        confidence=confidence,
        is_future=start > reference_time,
        source="llm",
    )
