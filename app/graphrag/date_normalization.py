"""日期写法 → 补零 ISO。

只服务写入层（ETL 的 convert_field_value，以及确认本体时的存量校验）。
查询层的具名区间是另一回事，在 date_periods.py。

**为什么必须补零**：图里的日期是字符串属性，范围过滤靠字典序。
'2026-1-5' 排在 '2026-10-05' 前面——不补零，一月五号就成了十月之后的日子，
而这个错误一声不吭：范围过滤漏行、排序错位，没有任何异常。
"""

from __future__ import annotations

import re
from datetime import date

#: 认的写法，一律四位年份在前——所以日/月谁在前不存在歧义。
#: 分隔符 - / . 三种，加中文年月日，加纯 8 位数字。
_YEAR_FIRST = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_CHINESE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")

#: 日或月在前的两位数写法。**单独识别出来只为了给一句说得清的报错**——
#: 03/04/2026 是三月四日还是四月三日，数据本身回答不了，猜错了不报错，
#: 只会让「三月的订单」静默答错。落进通用分支的话报错会是笼统的
#: 「认不出」，用户会以为是打错字。
_DAY_OR_MONTH_FIRST = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{4}$")


class InvalidDateValueError(ValueError):
    """认不出，或者格式合法但不是真实存在的日期。"""


def normalize_date(raw: str) -> str:
    """把无歧义的日期写法归一成 YYYY-MM-DD。"""
    text = raw.strip()
    for pattern in (_YEAR_FIRST, _CHINESE, _COMPACT):
        matched = pattern.match(text)
        if matched is None:
            continue
        year, month, day = (int(part) for part in matched.groups())
        try:
            # 真构造一次：2026-02-30 三段都在合法区间内，只有构造才拦得住。
            return date(year, month, day).isoformat()
        except ValueError:
            raise InvalidDateValueError(
                f"{raw!r} 不是一个真实存在的日期"
            ) from None
    if _DAY_OR_MONTH_FIRST.match(text):
        raise InvalidDateValueError(
            f"{raw!r} 有歧义：分不出是几月几日还是几日几月。"
            f"请改成年在前的写法，例如 2026-01-05。"
        )
    raise InvalidDateValueError(
        f"{raw!r} 认不出是日期。认得的写法：2026-01-05、2026/1/5、"
        f"2026.1.5、2026年1月5日、20260105。"
    )


def is_normalized_date(value: str) -> bool:
    """这个值**已经**是补零 ISO 了吗。

    不是「能不能归一」：'2026/1/5' 能归一，但它此刻长的样子仍然会破坏
    字典序。确认本体时用它判存量值合格与否——放行一个能归一但没归一的
    值，等于把问题留在数据里。
    """
    try:
        return normalize_date(value) == value
    except InvalidDateValueError:
        return False
