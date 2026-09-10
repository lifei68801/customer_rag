"""具名时间区间 → 一对绝对日期。

只服务查询层（structured_filter_query 的 in_period 算子）。写入侧的日期
归一化是另一回事，在 date_normalization.py——两者方向相反、调用方不同，
故意不合并。
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

#: 白名单。**「上个月」和「最近一个月」不是一回事**，这是这张表存在的理由：
#: 前者是自然月（last_month），后者是滚动窗口（last_30_days）。中文里用户说
#: 「上个月的订单」几乎总是指自然月。两种语义各有各的名字，就不会混。
#:
#: 这个元组同时喂给报错消息和 LLM 工具契约——两处各写一份的话，加了区间只
#: 改一处，另一处就开始推荐/接受一个对不上的集合。
PERIOD_NAMES: tuple[str, ...] = (
    "today",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
    "last_7_days",
    "last_30_days",
    "last_90_days",
)


class InvalidPeriodError(ValueError):
    """区间名不在 PERIOD_NAMES 里。"""


def _month_range(year: int, month: int) -> tuple[date, date]:
    """某个自然月的首末日。末日用 calendar.monthrange 取，不写死 28/30/31。"""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """把 (年, 月) 平移 delta 个月。

    先换算成"从 0 年 0 月起的月序号"再除模，不用 month + delta 直接算——
    后者在 1 月往前推时会得到 0 月。
    """
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _rolling(today: date, days: int) -> tuple[date, date]:
    """含今天在内往回 days 天。

    days - 1 而不是 days：7 天窗口是今天加上之前的 6 天。
    """
    return today - timedelta(days=days - 1), today


def resolve_period(name: str, *, today: date) -> tuple[str, str]:
    """把具名区间算成 (start, end) 两个 ISO 日期字符串，**两端都含**。

    two-ended inclusive 是因为调用方把它展开成 `>= start AND <= end`——
    半开区间要在展开处再减一天，那个减法迟早会漏写一次。

    `today` 是注入的，不是函数里 `date.today()`：跨年（1 月问 last_month 要
    得到去年 12 月）、月末（3 月 31 日问 last_month 要得到 2 月 1–28/29 日）、
    闰年这几条边界只有注入才钉得住。

    周起点是**周一**（国内业务惯例）。

    this_* 这几个的结束端取到周期末尾而不是今天：数据里通常没有未来日期，
    两种取法多半等价；但真有预约、排期这类未来日期时，取到今天会把它们
    漏掉，而用户问「本月的排期」时要的正是那些。
    """
    if name == "today":
        start, end = today, today
    elif name == "this_week":
        monday = today - timedelta(days=today.weekday())
        start, end = monday, monday + timedelta(days=6)
    elif name == "last_week":
        monday = today - timedelta(days=today.weekday() + 7)
        start, end = monday, monday + timedelta(days=6)
    elif name == "this_month":
        start, end = _month_range(today.year, today.month)
    elif name == "last_month":
        year, month = _shift_month(today.year, today.month, -1)
        start, end = _month_range(year, month)
    elif name == "this_quarter":
        first_month = (today.month - 1) // 3 * 3 + 1
        start = date(today.year, first_month, 1)
        end = _month_range(today.year, first_month + 2)[1]
    elif name == "last_quarter":
        first_month = (today.month - 1) // 3 * 3 + 1
        year, month = _shift_month(today.year, first_month, -3)
        start = date(year, month, 1)
        end = _month_range(*_shift_month(year, month, 2))[1]
    elif name == "this_year":
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
    elif name == "last_year":
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif name == "last_7_days":
        start, end = _rolling(today, 7)
    elif name == "last_30_days":
        start, end = _rolling(today, 30)
    elif name == "last_90_days":
        start, end = _rolling(today, 90)
    else:
        raise InvalidPeriodError(
            f"不认识的时间区间 {name!r}，可用的区间: {list(PERIOD_NAMES)}"
        )
    return start.isoformat(), end.isoformat()
