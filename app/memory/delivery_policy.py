from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

_WINDOW_SECONDS = 7 * 24 * 3600  # 统一按 7 天窗口计数，只有间隔/条数上限按画像调整

_DEFAULT_MIN_INTERVAL_SECONDS = 24 * 3600  # 默认最小间隔：1天
_DEFAULT_MAX_PER_WINDOW = 3

_TOO_PROACTIVE_MIN_INTERVAL_SECONDS = 3 * 24 * 3600  # 抱怨过"太主动"：拉长到3天
_TOO_PROACTIVE_MAX_PER_WINDOW = 1

_MORE_PROACTIVE_MIN_INTERVAL_SECONDS = 6 * 3600  # 反馈"希望更主动"：缩短到6小时
_MORE_PROACTIVE_MAX_PER_WINDOW = 10


@dataclass(frozen=True)
class DeliveryPolicy:
    min_interval_seconds: int
    max_per_window: int
    window_seconds: int = _WINDOW_SECONDS


def compute_delivery_policy(profile: dict[str, Any]) -> DeliveryPolicy:
    """根据客户画像的历史反馈标签动态调整跟进频率——一旦客户标记过"太
    主动了"，自动放宽间隔并降低单位时间窗口内的最大发送次数；反之则
    收紧间隔、提高上限。VIP 状态目前不参与频率调整（只影响文案语气，
    见 followup_engine.py），避免把"VIP=更少打扰"和"VIP=更多关注"这
    两种相反的合理解读都揉进同一个数字里，没有业务方明确要哪种就不猜。
    """
    feedback_label = profile.get("feedback_label", "neutral")
    if feedback_label == "too_proactive":
        return DeliveryPolicy(
            min_interval_seconds=_TOO_PROACTIVE_MIN_INTERVAL_SECONDS,
            max_per_window=_TOO_PROACTIVE_MAX_PER_WINDOW,
        )
    if feedback_label == "more_proactive":
        return DeliveryPolicy(
            min_interval_seconds=_MORE_PROACTIVE_MIN_INTERVAL_SECONDS,
            max_per_window=_MORE_PROACTIVE_MAX_PER_WINDOW,
        )
    return DeliveryPolicy(
        min_interval_seconds=_DEFAULT_MIN_INTERVAL_SECONDS,
        max_per_window=_DEFAULT_MAX_PER_WINDOW,
    )


def can_send_now(
    policy: DeliveryPolicy,
    *,
    send_history: list[datetime],
    now: datetime,
) -> bool:
    """判断现在能不能发一条主动跟进消息：既不能离上一次发送太近（最小
    间隔），也不能让窗口期内的发送总数超过上限——两个条件独立检查，
    任一不满足就不能发。
    """
    if send_history:
        last_sent = max(send_history)
        if (now - last_sent).total_seconds() < policy.min_interval_seconds:
            return False

    window_start = now - timedelta(seconds=policy.window_seconds)
    recent_count = sum(1 for sent_at in send_history if sent_at >= window_start)
    if recent_count >= policy.max_per_window:
        return False

    return True
