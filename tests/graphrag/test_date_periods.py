from __future__ import annotations

from datetime import date

import pytest

from app.graphrag.date_periods import (
    PERIOD_NAMES,
    InvalidPeriodError,
    resolve_period,
)


def test_last_month_crosses_the_year_boundary():
    """1 月问「上个月」要得到去年 12 月。

    用 today.month - 1 直接算的实现会在这里得到 0 月，要么抛 ValueError
    要么算出一个不存在的日期——两种都在这条用例上红。
    """
    assert resolve_period("last_month", today=date(2026, 1, 15)) == (
        "2025-12-01",
        "2025-12-31",
    )


def test_last_month_from_a_day_that_does_not_exist_in_that_month():
    """3 月 31 日问「上个月」要得到 2 月 1–28 日。

    用 today.replace(month=today.month - 1) 的实现会在这里抛
    ValueError: day is out of range for month。
    """
    assert resolve_period("last_month", today=date(2026, 3, 31)) == (
        "2026-02-01",
        "2026-02-28",
    )


def test_february_in_a_leap_year_ends_on_the_29th():
    assert resolve_period("last_month", today=date(2024, 3, 10)) == (
        "2024-02-01",
        "2024-02-29",
    )


def test_this_week_starts_on_monday():
    """周起点是周一，不是周日。2026-09-13 是周日——它属于 09-07 那一周。

    用周日当起点的实现会在这里给出 09-13 ~ 09-19。
    """
    assert resolve_period("this_week", today=date(2026, 9, 13)) == (
        "2026-09-07",
        "2026-09-13",
    )


def test_last_week_is_the_seven_days_before_this_week():
    assert resolve_period("last_week", today=date(2026, 9, 10)) == (
        "2026-08-31",
        "2026-09-06",
    )


def test_last_7_days_includes_today_and_spans_seven_days():
    """含今天在内共 7 天，不是 8 天。

    today - timedelta(days=7) 的实现会给出 09-03，那是 8 天。
    """
    assert resolve_period("last_7_days", today=date(2026, 9, 10)) == (
        "2026-09-04",
        "2026-09-10",
    )


def test_last_30_and_90_days():
    assert resolve_period("last_30_days", today=date(2026, 9, 10)) == (
        "2026-08-12",
        "2026-09-10",
    )
    assert resolve_period("last_90_days", today=date(2026, 9, 10)) == (
        "2026-06-13",
        "2026-09-10",
    )


def test_this_month_ends_at_the_end_of_the_month_not_today():
    """结束端取到月末而不是今天。

    数据里通常没有未来日期，两种取法在结果上多半等价——但真有预约/排期
    这类未来日期时，取到今天会把它们漏掉，而用户问「本月的排期」时要的
    正是那些。取到今天的实现在这里给出 09-10。
    """
    assert resolve_period("this_month", today=date(2026, 9, 10)) == (
        "2026-09-01",
        "2026-09-30",
    )


def test_quarters():
    assert resolve_period("this_quarter", today=date(2026, 9, 10)) == (
        "2026-07-01",
        "2026-09-30",
    )
    assert resolve_period("last_quarter", today=date(2026, 9, 10)) == (
        "2026-04-01",
        "2026-06-30",
    )


def test_last_quarter_crosses_the_year_boundary():
    """一季度问「上季度」要得到去年四季度。"""
    assert resolve_period("last_quarter", today=date(2026, 2, 10)) == (
        "2025-10-01",
        "2025-12-31",
    )


def test_years_and_today():
    assert resolve_period("this_year", today=date(2026, 9, 10)) == (
        "2026-01-01",
        "2026-12-31",
    )
    assert resolve_period("last_year", today=date(2026, 9, 10)) == (
        "2025-01-01",
        "2025-12-31",
    )
    assert resolve_period("today", today=date(2026, 9, 10)) == (
        "2026-09-10",
        "2026-09-10",
    )


def test_every_declared_name_resolves():
    """PERIOD_NAMES 和实现必须同步——名字列进去了却算不出来，
    报错消息会把一个不可用的值推荐给 LLM。"""
    for name in PERIOD_NAMES:
        start, end = resolve_period(name, today=date(2026, 9, 10))
        assert start <= end


def test_an_unknown_name_is_refused_and_the_message_lists_what_works():
    """报错要让 LLM 下一次能产出对的：只说「不支持」它还是不知道该填什么。"""
    with pytest.raises(InvalidPeriodError) as exc:
        resolve_period("last_fortnight", today=date(2026, 9, 10))
    assert "last_month" in str(exc.value)
