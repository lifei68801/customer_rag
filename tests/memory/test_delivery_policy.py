from datetime import datetime, timedelta

from app.memory.delivery_policy import can_send_now, compute_delivery_policy


def _profile(**overrides):
    base = {"is_vip": False, "feedback_label": "neutral", "communication_style": "formal"}
    base.update(overrides)
    return base


def test_neutral_profile_gets_default_policy():
    policy = compute_delivery_policy(_profile())

    assert policy.min_interval_seconds > 0
    assert policy.max_per_window > 0


def test_too_proactive_feedback_widens_interval_and_lowers_max():
    default_policy = compute_delivery_policy(_profile(feedback_label="neutral"))
    complained_policy = compute_delivery_policy(_profile(feedback_label="too_proactive"))

    assert complained_policy.min_interval_seconds > default_policy.min_interval_seconds
    assert complained_policy.max_per_window < default_policy.max_per_window


def test_more_proactive_feedback_narrows_interval_and_raises_max():
    default_policy = compute_delivery_policy(_profile(feedback_label="neutral"))
    wants_more_policy = compute_delivery_policy(_profile(feedback_label="more_proactive"))

    assert wants_more_policy.min_interval_seconds < default_policy.min_interval_seconds
    assert wants_more_policy.max_per_window > default_policy.max_per_window


def test_can_send_now_true_when_no_prior_sends():
    policy = compute_delivery_policy(_profile())
    now = datetime(2026, 8, 5, 10, 0, 0)

    assert can_send_now(policy, send_history=[], now=now) is True


def test_can_send_now_false_within_min_interval_of_last_send():
    policy = compute_delivery_policy(_profile())
    now = datetime(2026, 8, 5, 10, 0, 0)
    last_sent = now - timedelta(seconds=policy.min_interval_seconds - 1)

    assert can_send_now(policy, send_history=[last_sent], now=now) is False


def test_can_send_now_false_when_window_limit_reached():
    policy = compute_delivery_policy(_profile())
    now = datetime(2026, 8, 5, 10, 0, 0)
    # 全部在窗口内、但间隔都够长的历史发送记录，专门测试"窗口内条数上限"
    history = [
        now - timedelta(seconds=policy.min_interval_seconds * (i + 1))
        for i in range(policy.max_per_window)
    ]

    assert can_send_now(policy, send_history=history, now=now) is False
