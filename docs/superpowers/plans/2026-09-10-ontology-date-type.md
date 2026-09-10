# 本体日期类型 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给本体加 `date` value_type，让「上个月的订单」这类时间范围过滤在图谱层做得了。

**Architecture:** 日期在 Neo4j 里仍是字符串属性，值的不变量是补零的 `YYYY-MM-DD`——字典序即时间序，`>=` / `<=` 直接可用。相对时间由后端的具名区间算子 `in_period` 在校验之后**展开成两条普通约束**（`gte` + `lte`），图谱执行层一行不用改。写入侧由 ETL 归一化保证不变量，已有数据由确认本体时的一次校验守住。

**Tech Stack:** Python 3.12 / FastAPI / aiosqlite / Neo4j；前端 React 18 + TS + vitest

**Spec:** `docs/superpowers/specs/2026-09-10-ontology-date-type-design.md`

## Global Constraints

1. **图里存的日期值一律是补零的 `YYYY-MM-DD`，10 个字符。** 这是 `date` 类型存在的全部意义：字典序等于时间序。`2026-1-5` 混进来会排在 `2026-10-05` 前面，范围过滤漏行、排序错位，一个错误都不报。每条写入路径都必须保证它。
2. **`_VALID_STANDARD_NAME_VALUE_TYPES` 不加 `date`。** `standard_name` 是实体的名字，一个日期不该当实体的名字。两张白名单从此不对称，代码里要写明这是有意的。
3. **有歧义的日期写法一律拒，不猜。** `03/04/2026` 分不出三月四日还是四月三日，猜错了不报错，只会让「三月的订单」静默答错。
4. **`resolve_period` 的 `today` 是注入的参数**，不是函数里 `date.today()`——跨年、月末、闰年这几条边界只有注入才钉得住。
5. **不给 date 开 `starts_with`。** 有了 `in_period` 和绝对区间，第三种写法只会让 LLM 在三条路之间摇摆。
6. **确认本体时不合格就拒，不顺手改写图里的数据。** 一次「声明类型」的操作不该改数据。
7. **绝不使用 `git add -A` 或 `git add .`**，逐个文件点名。工作区里有用户刻意保留的未提交改动。
8. **不推送到 origin**，推送需要用户单独指示。
9. 后端 pytest 必须 `-u`，一次只跑一个，重定向到文件再读；前端 vitest 必须在 `frontend/` 目录下跑。
10. 注释里不许写未经验证的因果。
11. 打补丁的字符串替换必须 `assert old in s`。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/graphrag/date_periods.py`（新） | 具名区间 → `[start, end]`。查询层专用，纯函数 |
| `app/graphrag/date_normalization.py`（新） | 任意写法 → 补零 ISO。写入层专用，纯函数 |
| `app/graphrag/ontology_categories.py`（改） | `date` 进 extra_field 白名单 |
| `app/graphrag/neo4j_client.py`（改） | date 字段建索引；新增 `count_non_iso_date_values` |
| `app/graphrag/neptune_client.py`（改） | 新方法的 `NotImplementedError` 存根 |
| `app/graphrag/schema_etl_row_processing.py`（改） | `convert_field_value` 的 date 分支 |
| `app/graphrag/structured_filter_query.py`（改） | date 算子表 + `in_period` 展开 |
| `app/agent/tools/structured_filter_query/tool.py`（改） | 工具契约 enum + 说明 + 当前日期 |
| `app/api/admin_ontology_routes.py`（改） | 确认本体前校验存量日期值 |
| `frontend/src/admin/extraFieldDisplay.ts`（改） | `date` 的界面说法 |
| `frontend/src/admin/OntologySchemaPage.tsx`（改） | 类型下拉加 `date` |
| `frontend/src/admin/guidedOntology/draftProposal.ts`（改） | 日期列产出 `date` 而不是 `string` |
| `frontend/src/admin/guidedOntology/ProposalReview.tsx`（改） | 「日期列的限制」改写成说明 |

两个新模块**不合并成一个** `dates.py`：它们服务于完全相反的两个方向（读 vs 写），被完全不同的层调用，且永远不会一起用。合并只会让两边的调用方各自 import 一个用不到一半的模块。

---

## Task 1: 具名区间

**Files:**
- Create: `app/graphrag/date_periods.py`
- Test: `tests/graphrag/test_date_periods.py`

**Interfaces:**
- Produces:
  - `InvalidPeriodError(ValueError)`
  - `PERIOD_NAMES: tuple[str, ...]` —— 12 个区间名，给报错消息和工具契约共用
  - `resolve_period(name: str, *, today: date) -> tuple[str, str]` —— 返回 `(start_iso, end_iso)`，两端都含

- [ ] **Step 1: 写失败测试**

`tests/graphrag/test_date_periods.py`：

```python
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
```

- [ ] **Step 2: 运行，确认红**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/graphrag/test_date_periods.py > /tmp/t1.txt 2>&1
```
预期：`ModuleNotFoundError: No module named 'app.graphrag.date_periods'`

- [ ] **Step 3: 实现**

`app/graphrag/date_periods.py`：

```python
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
```

- [ ] **Step 4: 运行，确认绿**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/graphrag/test_date_periods.py > /tmp/t1.txt 2>&1
```

- [ ] **Step 5: 变异**

每条跑完把文件还原。

```
A：_shift_month 换成 (year, month - 1)        → 预期红 last_month_crosses_the_year_boundary
B：this_week 的 weekday() 换成 (weekday()+1)%7 → 预期红 this_week_starts_on_monday
C：_rolling 的 days - 1 换成 days              → 预期红 last_7_days_includes_today
D：this_month 的 end 换成 today                → 预期红 this_month_ends_at_the_end_of_the_month
E：PERIOD_NAMES 里加一个 "last_fortnight"      → 预期红 every_declared_name_resolves
```

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/date_periods.py tests/graphrag/test_date_periods.py
git commit -m "feat(date): 具名时间区间算成一对绝对日期"
```

---

## Task 2: 日期归一化

**Files:**
- Create: `app/graphrag/date_normalization.py`
- Test: `tests/graphrag/test_date_normalization.py`

**Interfaces:**
- Produces:
  - `InvalidDateValueError(ValueError)`
  - `normalize_date(raw: str) -> str` —— 返回补零 ISO
  - `is_normalized_date(value: str) -> bool` —— 「已经是补零 ISO」，Task 7 用它判存量值合格与否

- [ ] **Step 1: 写失败测试**

`tests/graphrag/test_date_normalization.py`：

```python
from __future__ import annotations

import pytest

from app.graphrag.date_normalization import (
    InvalidDateValueError,
    is_normalized_date,
    normalize_date,
)


def test_padding_is_what_makes_lexicographic_order_correct():
    """这个函数存在的**全部**理由：补零之后字典序才等于时间序。

    只断言相等的话，一个「原样返回」的实现能过掉一半用例。这条排序断言
    才是真正区分对错实现的那一条——不补零的话 '2026-1-5' > '2026-10-05'。
    """
    assert normalize_date("2026-1-5") == "2026-01-05"
    assert normalize_date("2026-1-5") < normalize_date("2026-10-05")


def test_the_five_accepted_spellings():
    """一律四位年份在前，所以日/月谁在前不存在歧义。"""
    assert normalize_date("2026-01-05") == "2026-01-05"
    assert normalize_date("2026/1/5") == "2026-01-05"
    assert normalize_date("2026.1.5") == "2026-01-05"
    assert normalize_date("2026年1月5日") == "2026-01-05"
    assert normalize_date("20260105") == "2026-01-05"


def test_surrounding_whitespace_is_tolerated():
    """CSV 里字段前后带空格很常见，为它拒一整行不值得。"""
    assert normalize_date("  2026-01-05 ") == "2026-01-05"


def test_a_well_formed_but_impossible_date_is_refused():
    """2026-02-30 格式合法但不是日期。不拦住它就会在图里躺一个永远
    排不对序的值——它排在 2026-03-01 前面，而它不存在。"""
    with pytest.raises(InvalidDateValueError):
        normalize_date("2026-02-30")


def test_an_ambiguous_spelling_is_refused_and_the_message_says_why():
    """03/04/2026 分不出三月四日还是四月三日。

    消息必须说出「有歧义」这个具体原因——只说「格式错误」的话，用户会
    以为是打错字，改成 3/4/2026 再试一次，仍然被拒，仍然不知道为什么。
    """
    with pytest.raises(InvalidDateValueError) as exc:
        normalize_date("03/04/2026")
    assert "歧义" in str(exc.value)


def test_day_first_with_dashes_is_also_refused():
    with pytest.raises(InvalidDateValueError):
        normalize_date("15-01-2026")


def test_an_excel_serial_number_is_refused():
    """Excel 序列号不认：convert_excel_cell_to_string 已经把日期单元格
    转成 %Y-%m-%d 了；序列号只在单元格没被格式化成日期时才冒出来，那时
    它跟一个普通整数在数据上无法区分，认它就是在猜。"""
    with pytest.raises(InvalidDateValueError):
        normalize_date("45678")


def test_free_text_is_refused():
    with pytest.raises(InvalidDateValueError):
        normalize_date("待定")


def test_is_normalized_date_only_accepts_the_padded_iso_form():
    """「能归一」不等于「已经合格」：2026/1/5 能归一，但它此刻长的样子
    仍然会破坏字典序。"""
    assert is_normalized_date("2026-01-05") is True
    assert is_normalized_date("2026-1-5") is False
    assert is_normalized_date("2026/01/05") is False
    assert is_normalized_date("待定") is False
    assert is_normalized_date("2026-02-30") is False
```

- [ ] **Step 2: 运行，确认红**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/graphrag/test_date_normalization.py > /tmp/t2.txt 2>&1
```

- [ ] **Step 3: 实现**

`app/graphrag/date_normalization.py`：

```python
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
```

- [ ] **Step 4: 运行，确认绿**

- [ ] **Step 5: 变异**

```
A：normalize_date 直接 return text（不补零）      → 预期红 padding_is_what_makes_lexicographic_order_correct
B：去掉 date(y,m,d) 构造，直接 f"{y}-{m:02d}-..."  → 预期红 a_well_formed_but_impossible_date_is_refused
C：把 _DAY_OR_MONTH_FIRST 那个分支删掉            → 预期红 an_ambiguous_spelling_is_refused_and_the_message_says_why
D：_YEAR_FIRST 放宽成 (\d{1,4})                   → 预期红 an_excel_serial_number_is_refused（45678 不匹配，仍红？见下）
E：is_normalized_date 改成「能归一就算合格」        → 预期红 is_normalized_date_only_accepts_the_padded_iso_form
```

变异 D 若结果为绿，说明这条放宽在现有用例下不可观测——那就补一条用例（例如 `normalize_date("26-1-5")` 应被拒，两位年份是 2026 还是 1926 说不清），而不是把变异记成无效。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/date_normalization.py tests/graphrag/test_date_normalization.py
git commit -m "feat(date): 日期写法归一成补零 ISO，有歧义的拒掉"
```

---

## Task 3: 数据模型接受 date

**Files:**
- Modify: `app/graphrag/ontology_categories.py:31`（白名单）、`:88`（异常 docstring）
- Modify: `app/graphrag/neo4j_client.py:1489`（`_SCALAR_VALUE_TYPES`）
- Test: `tests/graphrag/test_ontology_categories.py`、`tests/graphrag/test_neo4j_client.py`

**Interfaces:**
- Produces：`ExtraFieldSpec(value_type="date")` 可以被 `create_term_type` / `update_term_type` 接受并读写往返；`ensure_extra_field_indexes` 会给 date 字段建索引。

- [ ] **Step 1: 写失败测试**

追加到 `tests/graphrag/test_ontology_categories.py`：

```python
async def test_a_date_extra_field_round_trips():
    """date 是合法的属性类型，声明之后读回来还是 date。"""
    conn = await aiosqlite.connect(":memory:")
    try:
        await ensure_term_type_schema(conn)
        await create_term_type(
            conn, "demo", value="订单",
            extra_fields=[ExtraFieldSpec(name="purchase_date", value_type="date", label="下单日期")],
            actor="admin", status="draft",
        )
        types = await list_term_types(conn, "demo", status="draft")
        assert types[0].extra_fields[0].value_type == "date"
    finally:
        await conn.close()


async def test_standard_name_cannot_be_a_date():
    """standard_name 是实体的名字，一个日期不该当实体的名字。

    两张白名单从此不对称——这条用例是那份不对称的证据，防止有人
    「顺手」把 date 也加进 _VALID_STANDARD_NAME_VALUE_TYPES。
    """
    conn = await aiosqlite.connect(":memory:")
    try:
        await ensure_term_type_schema(conn)
        with pytest.raises(InvalidExtraFieldTypeError):
            await create_term_type(
                conn, "demo", value="订单", extra_fields=[],
                standard_name_value_type="date", actor="admin", status="draft",
            )
    finally:
        await conn.close()
```

（`ensure_term_type_schema` / `create_term_type` / `list_term_types` / `InvalidExtraFieldTypeError` 的确切导入名和现有 fixture 用法照抄同文件里已有的用例。）

追加到 `tests/graphrag/test_neo4j_client.py`（照该文件已有的 fake driver / session 录制手法）：

```python
async def test_date_fields_get_an_index():
    """漏了 date 的话功能照样对，只是每次范围过滤全表扫——而范围过滤
    恰恰是最需要索引的那类查询，压测之前谁也看不出来。"""
    client, recorded = _client_recording_cypher()
    await client.ensure_extra_field_indexes(
        tenant_id="demo", term_type="订单",
        extra_fields=[ExtraFieldSpec(name="purchase_date", value_type="date", label="")],
    )
    assert any("purchase_date" in q for q in recorded)
```

- [ ] **Step 2: 运行，确认红**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/graphrag/test_ontology_categories.py tests/graphrag/test_neo4j_client.py > /tmp/t3.txt 2>&1
```

- [ ] **Step 3: 实现**

`ontology_categories.py:31`：

```python
_VALID_EXTRA_FIELD_VALUE_TYPES = frozenset({"string", "number", "integer", "number[]", "date"})
# date 有意**不**进下面这张表：standard_name 是实体的名字，一个日期不该当
# 实体的名字。两张白名单从此不对称，这是有意的，不是漏了。
_VALID_STANDARD_NAME_VALUE_TYPES = frozenset({"string", "number", "integer"})
```

同文件 `:88` 那条异常的 docstring 里列举的类型补上 `"date"`。

`neo4j_client.py:1489`：

```python
        # date 也建索引：它在 Neo4j 里是字符串属性（字典序即时间序），
        # 范围过滤全指着这个索引。
        _SCALAR_VALUE_TYPES = {"string", "number", "integer", "date"}
```

- [ ] **Step 4: 运行，确认绿**

- [ ] **Step 5: 变异**

```
A：_VALID_EXTRA_FIELD_VALUE_TYPES 去掉 date  → 预期红 a_date_extra_field_round_trips
B：_VALID_STANDARD_NAME_VALUE_TYPES 加 date  → 预期红 standard_name_cannot_be_a_date
C：_SCALAR_VALUE_TYPES 不加 date             → 预期红 date_fields_get_an_index
```

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/ontology_categories.py app/graphrag/neo4j_client.py tests/graphrag/test_ontology_categories.py tests/graphrag/test_neo4j_client.py
git commit -m "feat(date): 本体属性接受 date 类型，并给它建索引"
```

---

## Task 4: ETL 写入日期字段

**Files:**
- Modify: `app/graphrag/schema_etl_row_processing.py:65-90`（`convert_field_value`）
- Test: `tests/graphrag/test_schema_etl_row_processing.py`

**Interfaces:**
- Consumes: `normalize_date` / `InvalidDateValueError`（Task 2）
- Produces: 声明成 `date` 的字段，写进图里的值一定是补零 ISO；认不出的值抛 `RowProcessingError`

- [ ] **Step 1: 写失败测试**

追加到 `tests/graphrag/test_schema_etl_row_processing.py`：

```python
def test_a_date_field_is_normalized_before_it_reaches_the_graph():
    """源数据里的 2026/1/15 写进图之前必须变成 2026-01-15。

    原样存的实现会让「三月的订单」这类范围过滤漏掉这一行，且不报错。
    """
    specs = {"purchase_date": ExtraFieldSpec(name="purchase_date", value_type="date", label="")}
    assert convert_field_value(
        extra_field_specs=specs, field_name="purchase_date", raw_value="2026/1/15",
    ) == "2026-01-15"


def test_an_unparseable_date_becomes_a_row_processing_error():
    """抛 RowProcessingError 才会进跳过行明细——管理员在报错明细页看得见
    是哪一行、哪个值。抛别的异常会穿透成 500，整份导入挂掉。"""
    specs = {"purchase_date": ExtraFieldSpec(name="purchase_date", value_type="date", label="")}
    with pytest.raises(RowProcessingError) as exc:
        convert_field_value(
            extra_field_specs=specs, field_name="purchase_date", raw_value="03/04/2026",
        )
    # 原因要原样透出去，不能包装成一句「转换失败」——那句话回答不了
    # 「我该把这一列改成什么样」。
    assert "歧义" in str(exc.value)
```

- [ ] **Step 2: 运行，确认红**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/graphrag/test_schema_etl_row_processing.py > /tmp/t4.txt 2>&1
```

- [ ] **Step 3: 实现**

`schema_etl_row_processing.py`，顶部加导入：

```python
from app.graphrag.date_normalization import InvalidDateValueError, normalize_date
```

`convert_field_value` 的 try 块里，在 `number[]` 分支之后加：

```python
        if value_type == "date":
            # 归一化的理由见 date_normalization 模块文档：图里的日期是字符串，
            # 范围过滤靠字典序，不补零就排错序而且一声不吭。
            #
            # 这个分支放在 try/except ValueError 里面是安全的：
            # InvalidDateValueError 是 ValueError 的子类，会被下面那个
            # except 接住——但那句话太笼统（"无法转换成声明的类型"），
            # 丢掉了"为什么"。所以在这里就地转成 RowProcessingError。
            try:
                return normalize_date(raw_value)
            except InvalidDateValueError as exc:
                raise RowProcessingError(f"字段 {field_name!r}：{exc}") from None
```

- [ ] **Step 4: 运行，确认绿**

- [ ] **Step 5: 变异**

```
A：date 分支改成 return raw_value              → 预期红 a_date_field_is_normalized_before_it_reaches_the_graph
B：去掉就地的 except，让它落进通用 except ValueError → 预期红 an_unparseable_date_becomes_a_row_processing_error（"歧义" 断言）
```

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/schema_etl_row_processing.py tests/graphrag/test_schema_etl_row_processing.py
git commit -m "feat(date): ETL 写日期字段前先归一，认不出的进跳过行明细"
```

---

## Task 5: 查询层的 in_period

**Files:**
- Modify: `app/graphrag/structured_filter_query.py`（`:40-49` 算子表、`:634` `run_structured_filter_query`，新增 `_expand_period_constraints`）
- Test: `tests/graphrag/test_structured_filter_query.py`

**Interfaces:**
- Consumes: `resolve_period` / `InvalidPeriodError` / `PERIOD_NAMES`（Task 1）；`date` value_type（Task 3）
- Produces: `run_structured_filter_query(..., today: date | None = None)`；`in_period` 约束在到达图谱层之前已被展开成 `gte` + `lte` 两条

- [ ] **Step 1: 写失败测试**

追加到 `tests/graphrag/test_structured_filter_query.py`（照该文件已有的 `term_type_schema` fixture 和 fake graph client 手法；fake 要能把收到的 `args.constraints` 录下来）：

```python
async def test_in_period_is_expanded_into_two_plain_constraints():
    """in_period 在到达图谱层之前就被展开成 gte + lte。

    这是整个设计的支点：图谱执行层一行不用改，新语义完全落在校验+展开
    这一层。不展开的话 Neo4j 那边会拿到一个它不认识的运算符。
    """
    graph = _RecordingGraphClient()
    await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单"},
            "constraints": [
                {"kind": "attribute", "field": "purchase_date",
                 "operator": "in_period", "value": "last_month"},
            ],
        },
        graph_client=graph, tenant_id="demo", terms=[],
        confirmed_relation_types=set(), term_type_schema=_SCHEMA_WITH_DATE,
        today=date(2026, 9, 10),
    )
    assert [(c.field, c.operator, c.value) for c in graph.last_args.constraints] == [
        ("purchase_date", "gte", "2026-08-01"),
        ("purchase_date", "lte", "2026-08-31"),
    ]


async def test_in_period_on_a_relation_constraint_keeps_the_same_hops():
    """关系约束里的 in_period 展开成两条 hops 相同的约束。

    只展开属性约束的实现在这里会把一个图谱层不认识的 target_operator
    直接送下去。
    """
    graph = _RecordingGraphClient()
    await run_structured_filter_query(
        {
            "anchor": {"term_type": "客户"},
            "constraints": [
                {"kind": "relation",
                 "hops": [{"relation_type": "HAS_ORDER", "direction": "outgoing",
                           "target_term_type": "订单"}],
                 "target_field": "purchase_date",
                 "target_operator": "in_period", "target_value": "today"},
            ],
        },
        graph_client=graph, tenant_id="demo", terms=[],
        confirmed_relation_types={"HAS_ORDER"}, term_type_schema=_SCHEMA_WITH_DATE,
        today=date(2026, 9, 10),
    )
    expanded = graph.last_args.constraints
    assert [(c.target_operator, c.target_value) for c in expanded] == [
        ("gte", "2026-09-10"), ("lte", "2026-09-10"),
    ]
    assert expanded[0].hops == expanded[1].hops


async def test_an_unknown_period_name_is_refused_with_the_list():
    """报错要让 LLM 下一次能产出对的。"""
    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单"},
            "constraints": [
                {"kind": "attribute", "field": "purchase_date",
                 "operator": "in_period", "value": "last_fortnight"},
            ],
        },
        graph_client=_RecordingGraphClient(), tenant_id="demo", terms=[],
        confirmed_relation_types=set(), term_type_schema=_SCHEMA_WITH_DATE,
        today=date(2026, 9, 10),
    )
    assert "last_month" in result["error"]


async def test_starts_with_is_not_available_on_a_date_field():
    """starts_with('2026-08') 能表达「8 月」，但那是把日期当字符串用的
    旁门。第三种写法只会让 LLM 在三条路之间摇摆，而它们的边界行为不一致
    （starts_with 表达不了跨月区间）。"""
    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单"},
            "constraints": [
                {"kind": "attribute", "field": "purchase_date",
                 "operator": "starts_with", "value": "2026-08"},
            ],
        },
        graph_client=_RecordingGraphClient(), tenant_id="demo", terms=[],
        confirmed_relation_types=set(), term_type_schema=_SCHEMA_WITH_DATE,
        today=date(2026, 9, 10),
    )
    assert "error" in result


async def test_in_period_is_not_available_on_a_number_field():
    """「上个月的售价」没有意义。"""
    result = await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单"},
            "constraints": [
                {"kind": "attribute", "field": "amount",
                 "operator": "in_period", "value": "last_month"},
            ],
        },
        graph_client=_RecordingGraphClient(), tenant_id="demo", terms=[],
        confirmed_relation_types=set(), term_type_schema=_SCHEMA_WITH_DATE,
        today=date(2026, 9, 10),
    )
    assert "error" in result


async def test_absolute_range_on_a_date_field_still_works():
    """任意窗口（「最近 45 天」）走绝对区间——那条路径必须留着。"""
    graph = _RecordingGraphClient()
    await run_structured_filter_query(
        {
            "anchor": {"term_type": "订单"},
            "constraints": [
                {"kind": "attribute", "field": "purchase_date",
                 "operator": "gte", "value": "2026-07-27"},
            ],
        },
        graph_client=graph, tenant_id="demo", terms=[],
        confirmed_relation_types=set(), term_type_schema=_SCHEMA_WITH_DATE,
        today=date(2026, 9, 10),
    )
    assert graph.last_args.constraints[0].operator == "gte"
```

`_SCHEMA_WITH_DATE` 是一个 `dict[str, TermTypeCategory]`，`订单` 带 `purchase_date`（`date`）和 `amount`（`number`）两个 extra field，`客户` 带一个空 extra_fields——照该文件已有 schema fixture 的构造方式写。

- [ ] **Step 2: 运行，确认红**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/graphrag/test_structured_filter_query.py > /tmp/t5.txt 2>&1
```

- [ ] **Step 3: 实现**

顶部导入：

```python
from datetime import date as _date

from app.graphrag.date_periods import InvalidPeriodError, resolve_period
```

`:40-49` 算子表：

```python
_STRING_OPERATORS = frozenset({"eq", "ne", "starts_with"})
_NUMERIC_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq", "ne"})
_ARRAY_OPERATORS = frozenset({"all_lte", "all_gte", "any_lte", "any_gte"})
# 日期在图里是字符串属性，字典序即时间序，所以数值那套比较运算符原样可用。
# in_period 是相对时间的入口（「上个月」），它在校验之后被展开成
# gte + lte 两条普通约束，图谱执行层不认识它。
#
# **有意不给 date 开 starts_with**：starts_with('2026-08') 能表达「8 月」，
# 但那是把日期当字符串用的旁门，而且表达不了跨月区间。有了 in_period 和
# 绝对区间，第三种写法只会让 LLM 在三条路之间摇摆。
_DATE_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq", "ne", "in_period"})
_VALID_OPERATORS = (
    _STRING_OPERATORS | _NUMERIC_OPERATORS | _ARRAY_OPERATORS | _DATE_OPERATORS
)
_OPERATORS_BY_VALUE_TYPE = {
    "string": _STRING_OPERATORS,
    "number": _NUMERIC_OPERATORS,
    "integer": _NUMERIC_OPERATORS,
    "number[]": _ARRAY_OPERATORS,
    "date": _DATE_OPERATORS,
}
```

新增展开函数（放在 `_resolve_fuzzy_constraint_values` 附近）：

```python
def _expand_period_constraints(
    constraints: list[AttributeConstraint | RelationConstraint], *, today: _date,
) -> list[AttributeConstraint | RelationConstraint]:
    """把每条 in_period 约束换成两条普通约束（gte start + lte end）。

    在校验通过之后、执行之前跑。这样 neo4j_client 一行不用改：
    _COMPARISON_OPERATOR_TO_CYPHER 已经有 >= 和 <=，多条约束本来就是
    " AND ".join(where_clauses)。新语义完全落在这一层，图谱执行层的
    攻击面不变。

    区间两端都含，所以是 gte/lte 而不是 gte/lt——resolve_period 返回的
    end 是区间最后一天本身。
    """
    expanded: list[AttributeConstraint | RelationConstraint] = []
    for constraint in constraints:
        if isinstance(constraint, AttributeConstraint):
            if constraint.operator != "in_period":
                expanded.append(constraint)
                continue
            start, end = resolve_period(str(constraint.value), today=today)
            expanded.append(replace(constraint, operator="gte", value=start))
            expanded.append(replace(constraint, operator="lte", value=end))
            continue
        if constraint.target_operator != "in_period":
            expanded.append(constraint)
            continue
        start, end = resolve_period(str(constraint.target_value), today=today)
        expanded.append(replace(constraint, target_operator="gte", target_value=start))
        expanded.append(replace(constraint, target_operator="lte", target_value=end))
    return expanded
```

`run_structured_filter_query` 签名加参数：

```python
async def run_structured_filter_query(
    raw_args: dict,
    *,
    graph_client: "Neo4jGraphClient",
    tenant_id: str,
    terms: list[Term],
    confirmed_relation_types: set[str],
    term_type_schema: dict[str, TermTypeCategory],
    allowed_combinations: list[AllowedCombination] | None = None,
    today: _date | None = None,
) -> dict[str, Any]:
```

在 `_resolve_fuzzy_constraint_values` 那一段**之后**插入展开（顺序有意：模糊值解析只针对 `standard_name` 字段，跟日期无关；展开放后面可以少走一遍解析）：

```python
    try:
        args = replace(args, constraints=_expand_period_constraints(
            args.constraints, today=today or _date.today(),
        ))
    except InvalidPeriodError as exc:
        return {"error": str(exc)}
```

- [ ] **Step 4: 运行，确认绿**

- [ ] **Step 5: 变异**

```
A：_expand_period_constraints 里跳过 RelationConstraint 分支 → 预期红 in_period_on_a_relation_constraint
B：展开成 gte + lt（半开区间）                              → 预期红 in_period_is_expanded_into_two_plain_constraints
C：_DATE_OPERATORS 加 starts_with                          → 预期红 starts_with_is_not_available_on_a_date_field
D：_OPERATORS_BY_VALUE_TYPE["number"] 加 in_period          → 预期红 in_period_is_not_available_on_a_number_field
E：InvalidPeriodError 不捕获，让它往上抛                     → 预期红 an_unknown_period_name_is_refused_with_the_list
```

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/structured_filter_query.py tests/graphrag/test_structured_filter_query.py
git commit -m "feat(date): 查询层支持 in_period，展开成两条普通约束"
```

---

## Task 6: LLM 工具契约

**Files:**
- Modify: `app/agent/tools/structured_filter_query/tool.py:12`（`_USAGE_GUIDE`）、`:96` 和 `:120`（两个 operator enum）、`:185`（`_build_prompt`）、`:241`（`execute`）
- Test: `tests/agent/tools/test_structured_filter_query.py`

**Interfaces:**
- Consumes: `PERIOD_NAMES`（Task 1）；`run_structured_filter_query(..., today=...)`（Task 5）
- Produces: 工具描述里带**调用时刻**的当前日期；enum 含 `in_period`

- [ ] **Step 1: 写失败测试**

追加到 `tests/agent/tools/test_structured_filter_query.py`：

```python
def test_the_operator_enum_offers_in_period():
    """LLM 只会产出 enum 里有的运算符。"""
    props = _PARAMETERS_SCHEMA["properties"]["constraints"]["items"]["properties"]
    assert "in_period" in props["operator"]["enum"]
    assert "in_period" in props["target_operator"]["enum"]


def test_the_usage_guide_lists_every_period_name():
    """PERIOD_NAMES 和说明里的清单必须同步——列漏一个，那个区间对 LLM
    就等于不存在；多列一个，LLM 会产出后端拒绝的值。"""
    for name in PERIOD_NAMES:
        assert name in _USAGE_GUIDE


def test_the_prompt_carries_the_date_of_this_call_not_of_process_start():
    """当前日期必须在**调用时**注入。

    写成模块级常量的话它会冻结在 import 那一刻——服务器连跑几天之后，
    「今天」还是启动那天，而这个错误一声不吭：LLM 照着一个过期的日期
    算出的绝对区间，看上去完全正常。
    """
    prompt = _build_prompt(
        query_intent="上个月的订单", original_question="上个月的订单有多少",
        candidates=[], today=date(2026, 9, 10),
    )
    assert "2026-09-10" in prompt
```

- [ ] **Step 2: 运行，确认红**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/agent/tools/test_structured_filter_query.py > /tmp/t6.txt 2>&1
```

- [ ] **Step 3: 实现**

顶部导入：

```python
from datetime import date

from app.graphrag.date_periods import PERIOD_NAMES
```

`_USAGE_GUIDE` 末尾追加一段（它是拼接的字符串常量，加一个 f-string 片段）：

```python
    f"日期类型的字段支持按时间范围过滤。相对时间用 operator=in_period，"
    f"value 填下面这些具名区间之一：{'、'.join(PERIOD_NAMES)}。\n"
    "注意「上个月」和「最近一个月」不是一回事：前者是自然月（last_month），"
    "后者是滚动窗口（last_30_days）。用户说「上个月的订单」通常指自然月。\n"
    "这些区间之外的窗口（比如「最近 45 天」），用 gte/lte 填绝对日期"
    "（YYYY-MM-DD），当前日期见下方。\n"
```

两个 enum（`:96` 的 `operator` 和 `:120` 的 `target_operator`）各加 `"in_period"`，描述里补一句「in_period 只对日期类型字段可用」。

`_build_prompt` 加参数并注入日期：

```python
def _build_prompt(query_intent: str, original_question: str, candidates, *, today: date) -> str:
    schema_text = json.dumps(_PARAMETERS_SCHEMA, ensure_ascii=False, indent=2)
    return (
        ...
        f"使用说明：\n{_USAGE_GUIDE}\n\n"
        # 当前日期在**调用时**注入，不是模块级常量：常量会冻结在 import
        # 那一刻，服务器连跑几天之后「今天」还是启动那天，而 LLM 照着它
        # 算出的绝对区间看上去完全正常。
        f"当前日期：{today.isoformat()}\n\n"
        ...
    )
```

调用 `_build_prompt` 的地方传 `today=date.today()`；`execute`（`:241`）把同一个值传给 `run_structured_filter_query(..., today=...)`。

> **实现注意**：`execute` 和参数生成是两次调用，若分别取 `date.today()`，跨零点时两者会不一致（LLM 按今天算，后端按明天算）。在 `execute` 开头取一次 `today = date.today()`，两处共用。

- [ ] **Step 4: 运行，确认绿**

- [ ] **Step 5: 变异**

```
A：enum 去掉 in_period                          → 预期红 the_operator_enum_offers_in_period
B：说明里手抄 11 个区间名（漏 last_90_days）      → 预期红 the_usage_guide_lists_every_period_name
C：日期写成模块级 _TODAY = date.today() 常量     → 预期红 the_prompt_carries_the_date_of_this_call（用 _build_prompt 的参数断言）
```

变异 C 若因为「常量恰好也是今天」而为绿，说明这条只有在跨天时才可观测——那就把断言改成传一个**非今天**的 `today`（如 `date(2020, 1, 1)`）并断言它出现在 prompt 里，而不是把变异记成无效。

- [ ] **Step 6: 提交**

```bash
git add app/agent/tools/structured_filter_query/tool.py tests/agent/tools/test_structured_filter_query.py
git commit -m "feat(date): 工具契约支持 in_period，并在调用时注入当前日期"
```

---

## Task 7: 确认本体时校验存量

**Files:**
- Modify: `app/graphrag/neo4j_client.py`（新增 `count_non_iso_date_values`，并加进 `GraphWriteProtocol`，`:742` 起）
- Modify: `app/graphrag/neptune_client.py`（`NotImplementedError` 存根）
- Modify: `app/api/admin_ontology_routes.py:599`（`confirm_tenant_ontology`）
- Test: `tests/api/test_admin_ontology_routes.py`

**Interfaces:**
- Consumes: `is_normalized_date`（Task 2）；`list_term_types(conn, tenant_id, status=...)`
- Produces: `count_non_iso_date_values(*, tenant_id: str, term_type: str, field: str) -> tuple[int, list[str]]`

- [ ] **Step 1: 写失败测试**

追加到 `tests/api/test_admin_ontology_routes.py`。`_FakeGraphClient` 加：

```python
        self.non_iso_by_field: dict[str, tuple[int, list[str]]] = {}
        self.date_scan_calls: list[tuple[str, str]] = []

    async def count_non_iso_date_values(
        self, *, tenant_id: str, term_type: str, field: str
    ) -> tuple[int, list[str]]:
        self.date_scan_calls.append((term_type, field))
        return self.non_iso_by_field.get(field, (0, []))
```

用例：

```python
def test_confirming_a_new_date_field_with_dirty_values_is_refused(client, ...):
    """图里还躺着 2026/1/15 这类值时不能确认。

    放行的话这些实体在按时间过滤时会被静默漏掉——字典序把它们排到了
    十月之后，而没有任何地方会报错。
    """
    graph = _FakeGraphClient()
    graph.non_iso_by_field["purchase_date"] = (12, ["2026/1/15", "待定"])
    app.dependency_overrides[deps.get_graph_client] = lambda: graph
    # 草稿里把 purchase_date 从 string 改成 date（已确认版本里它是 string）
    ...
    response = client.post("/api/admin/ontology/demo/confirm")
    assert response.status_code == 409
    detail = response.json()["detail"]
    # 数量和样例都要有：只说"有不合格的值"回答不了"我该去修什么"。
    assert "12" in detail and "2026/1/15" in detail and "purchase_date" in detail


def test_confirming_a_new_date_field_with_clean_values_goes_through(client, ...):
    graph = _FakeGraphClient()  # non_iso_by_field 空 → 一律 (0, [])
    ...
    assert client.post("/api/admin/ontology/demo/confirm").status_code == 200


def test_a_field_that_was_already_a_date_is_not_rescanned(client, ...):
    """没变的字段不查图——否则每次确认本体都要扫一遍全图。

    用 date_scan_calls 断言空，而不是断言"确认成功"：后者在"每次都扫、
    但恰好没有脏值"的实现下也是绿的。
    """
    graph = _FakeGraphClient()
    # 已确认版本里 purchase_date 就已经是 date，草稿没改它
    ...
    client.post("/api/admin/ontology/demo/confirm")
    assert graph.date_scan_calls == []


def test_a_brand_new_term_type_with_a_date_field_is_not_scanned(client, ...):
    """新建的实体类型图里一个节点都没有，扫它是白扫。

    这条同时挡住"只要草稿里有 date 字段就扫"的实现。
    """
    graph = _FakeGraphClient()
    # 草稿里新增一个已确认版本里不存在的 term_type，带 date 字段
    ...
    client.post("/api/admin/ontology/demo/confirm")
    assert graph.date_scan_calls == []
```

- [ ] **Step 2: 运行，确认红**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests/api/test_admin_ontology_routes.py > /tmp/t7.txt 2>&1
```

- [ ] **Step 3: 实现**

`neo4j_client.py`，`GraphWriteProtocol` 里加：

```python
    async def count_non_iso_date_values(
        self, *, tenant_id: str, term_type: str, field: str
    ) -> tuple[int, list[str]]: ...
```

`Neo4jGraphClient` 实现：

```python
    async def count_non_iso_date_values(
        self, *, tenant_id: str, term_type: str, field: str
    ) -> tuple[int, list[str]]:
        """这个租户这个类型下，该字段的值不是补零 ISO 的有几个，外加最多 3 个样例。

        判据是「已经是补零 ISO」，不是「能不能归一」：2026/1/15 能归一，但它
        此刻躺在图里的样子仍然会破坏字典序，放行等于把问题留在数据里。

        字段名不拼进 Cypher 文本，走属性访问的参数化写法——field 来自本体
        声明，虽然 _EXTRA_FIELD_NAME_PATTERN 已经限制了字符集，但拼字符串
        是这一层不该开的口子。
        """
        ...
```

> **实现注意**：Neo4j 不支持用参数做属性名。这里用 `t[$field]` 的动态属性访问语法（Neo4j 5 支持）。若目标版本不支持，退回「先按 `_EXTRA_FIELD_NAME_PATTERN` 断言 field 合法，再拼进语句」，并在注释里写明这是经过校验的白名单字符集、不是无条件拼接。实现时实测确认走哪条。

判定逻辑在 Python 侧用 `is_normalized_date`（Cypher 里写正则会让「合格」的定义分裂成两份，两份迟早不一致）：取回该字段的全部去重值，逐个判。

`neptune_client.py` 加对应的 `NotImplementedError` 存根（照该文件已有 16 个存根的写法）。

`admin_ontology_routes.py:599`：

```python
async def confirm_tenant_ontology(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    graph_client: GraphWriteProtocol = Depends(deps.get_graph_client),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await _assert_new_date_fields_have_clean_values(
        review_conn, graph_client, tenant_id=tenant_id,
    )
    await confirm_ontology(review_conn, tenant_id, actor=session.username)
    return {"confirmed": True}
```

新增 helper：

```python
async def _assert_new_date_fields_have_clean_values(
    review_conn: aiosqlite.Connection, graph_client: GraphWriteProtocol, *, tenant_id: str,
) -> None:
    """确认之前，挡住「字段刚变成 date、但图里还躺着非 ISO 值」这种情况。

    只查**这次从非 date 变成 date** 的字段：
    - 没变的不查，否则每次确认本体都要扫一遍全图；
    - 已确认版本里不存在的 term_type 不查，那是新建的类型，图里一个节点
      都没有。

    这是「查询开始照着新类型跑」的唯一分界点：update_term_type 写的是草稿，
    只有 confirm 会把它提升成 confirmed。
    """
    draft = {t.value: t for t in await list_term_types(review_conn, tenant_id, status="draft")}
    if not draft:
        # confirm_ontology 对空草稿是 no-op（幂等），这里同样直接放行。
        return
    confirmed = {t.value: t for t in await list_term_types(review_conn, tenant_id, status="confirmed")}
    problems: list[str] = []
    for value, term_type in draft.items():
        previous = confirmed.get(value)
        if previous is None:
            continue
        was_date = {f.name for f in previous.extra_fields if f.value_type == "date"}
        for spec in term_type.extra_fields:
            if spec.value_type != "date" or spec.name in was_date:
                continue
            count, samples = await graph_client.count_non_iso_date_values(
                tenant_id=tenant_id, term_type=value, field=spec.name,
            )
            if count:
                sample_text = "、".join(repr(s) for s in samples)
                problems.append(
                    f"{value}.{spec.name} 还有 {count} 个实体的值不是 YYYY-MM-DD，例如 {sample_text}"
                )
    if problems:
        raise HTTPException(
            status_code=409,
            detail="；".join(problems)
            + "。改成日期类型之前先重新导入这份数据，否则这些实体在按时间过滤时会被静默漏掉。",
        )
```

- [ ] **Step 4: 运行，确认绿**

- [ ] **Step 5: 变异**

```
A：detail 里去掉 count 和 samples，只留一句「有不合格的值」 → 预期红 confirming_a_new_date_field_with_dirty_values
B：不比对 was_date，草稿里每个 date 字段都扫                → 预期红 a_field_that_was_already_a_date_is_not_rescanned
C：previous is None 时不 continue，照样扫                   → 预期红 a_brand_new_term_type_with_a_date_field_is_not_scanned
D：count > 0 时不抛，只记日志                              → 预期红 confirming_a_new_date_field_with_dirty_values
```

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/neo4j_client.py app/graphrag/neptune_client.py app/api/admin_ontology_routes.py tests/api/test_admin_ontology_routes.py
git commit -m "feat(date): 确认本体前先校验存量日期值，不合格就 409 点名"
```

---

## Task 8: 前端

**Files:**
- Modify: `frontend/src/admin/extraFieldDisplay.ts:37-49`（两张标签表）
- Modify: `frontend/src/admin/OntologySchemaPage.tsx:101`（`VALUE_TYPES`）
- Modify: `frontend/src/admin/guidedOntology/draftProposal.ts:130-142`（`measureValueType`）
- Modify: `frontend/src/admin/guidedOntology/ProposalReview.tsx:337`（「日期列的限制」那一节）
- Test: `frontend/src/admin/valueTypeLabels.test.tsx`、`frontend/src/admin/guidedOntology/draftProposal.test.ts`、`frontend/src/admin/guidedOntology/proposalReview.test.tsx`

**Interfaces:**
- Consumes: 后端接受 `value_type: "date"`（Task 3）

- [ ] **Step 1: 写失败测试**

`draftProposal.test.ts` 追加：

```ts
describe('日期列', () => {
  it('日期列产出 date 类型，不是 string', () => {
    // 存成 string 的话，「上个月的订单」在图谱层做不了范围过滤——这正是
    // 这次改造要修的那件事。
    const roled = demoColumns()
    const proposal = buildProposal(roled, initialDecision(roled))
    const host = proposal.termTypes.find((t) => t.value === '订单号')
    const field = host?.extra_fields.find((f) => f.label === 'purchase_date')
    expect(field?.value_type).toBe('date')
  })
})
```

`valueTypeLabels.test.tsx` 追加：

```tsx
it('date 有中文说法，不是裸的 date', () => {
  // 认不出的枚举值会原样显示，用户在下拉里看到的就是 "date"。
  expect(valueTypeLabel('date')).toBe('日期')
  expect(valueTypeOptionLabel('date')).toMatch(/2026-01-05/)
})
```

`proposalReview.test.tsx` 追加：

```tsx
it('日期列那一节说的是能做什么，不再是「做不了范围过滤」', async () => {
  renderReview()
  const section = await screen.findByTestId('date-columns')
  expect(section.textContent).toMatch(/上个月/)
  expect(section.textContent).not.toMatch(/做不了/)
  // 认得的写法要列出来：用户在导入前就该知道 03/04/2026 会被跳过。
  expect(section.textContent).toMatch(/2026-01-05/)
})
```

（`ProposalReview.tsx` 那一节需要加 `data-testid="date-columns"`。）

- [ ] **Step 2: 运行，确认红**

```bash
cd frontend && npx vitest run src/admin/guidedOntology/draftProposal.test.ts src/admin/valueTypeLabels.test.tsx src/admin/guidedOntology/proposalReview.test.tsx
```

- [ ] **Step 3: 实现**

`extraFieldDisplay.ts`：两张表各加一行。

```ts
const VALUE_TYPE_SHORT_LABELS: Record<string, string> = {
  string: '文本',
  number: '小数',
  integer: '整数',
  'number[]': '小数列表',
  date: '日期',
}

const VALUE_TYPE_OPTION_LABELS: Record<string, string> = {
  string: '文本',
  number: '小数（如 19.99，售价/金额）',
  integer: '整数',
  'number[]': '小数列表',
  date: '日期（如 2026-01-05，可以按「上个月」这类时间范围过滤）',
}
```

`OntologySchemaPage.tsx:101`：

```ts
const VALUE_TYPES = ['string', 'number', 'integer', 'number[]', 'date'] as const
```

`STANDARD_NAME_VALUE_TYPES`（第 103 行）**不动**——理由同后端白名单。

`draftProposal.ts:130-142`，`measureValueType`：

```ts
function measureValueType(column: RoledColumn): DraftExtraField['value_type'] {
  if (column.role === 'date') return 'date'
  if (column.role === 'dimension') return 'string'
  ...
}
```

删掉那条「数据模型只有 string/number/integer/number[]，没有日期类型。这不是疏忽，是必须向用户明说的限制」的注释——它现在是假的。

`DraftExtraField['value_type']` 的联合类型加 `'date'`（在 `guidedOntology/types.ts`）。

`ProposalReview.tsx:337` 那一节改写：

```tsx
<section data-testid="date-columns" className="flex flex-col gap-3">
  <h2 className={sectionTitle}>日期列</h2>
  <p className="text-sm text-ink-soft">
    这些列会存成日期类型，可以按时间范围提问（「上个月的」「今年以来的」）。
    导入时认得这几种写法：2026-01-05、2026/1/5、2026.1.5、2026年1月5日、20260105。
    像 03/04/2026 这种日和月分不出先后的写法会被跳过并记进报错明细——
    分不出三月四日还是四月三日，猜错了不会报错，只会让「三月的订单」静默答错。
  </p>
  ...
</section>
```

- [ ] **Step 4: 运行，确认绿 + tsc**

```bash
cd frontend && npx tsc --noEmit && npx vitest run src
```

- [ ] **Step 5: 变异**

```
A：measureValueType 的 date 分支改回 'string'   → 预期红 日期列产出 date 类型
B：VALUE_TYPE_OPTION_LABELS 不加 date          → 预期红 date 有中文说法
C：ProposalReview 那节保留「做不了范围过滤」     → 预期红 日期列那一节说的是能做什么
D：STANDARD_NAME_VALUE_TYPES 也加 date         → 后端会 400；这条不该有前端用例，
                                                 由 Task 3 的 standard_name_cannot_be_a_date 守着
```

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/extraFieldDisplay.ts frontend/src/admin/OntologySchemaPage.tsx frontend/src/admin/guidedOntology/draftProposal.ts frontend/src/admin/guidedOntology/types.ts frontend/src/admin/guidedOntology/ProposalReview.tsx frontend/src/admin/valueTypeLabels.test.tsx frontend/src/admin/guidedOntology/draftProposal.test.ts frontend/src/admin/guidedOntology/proposalReview.test.tsx
git commit -m "feat(date): 界面上日期是一个可选类型，不再是一条限制"
```

---

## Task 9: 收尾验证

**Files:**
- Modify: `docs/superpowers/MANUAL-VERIFICATION.md`

- [ ] **Step 1: 全量**

```bash
.venv/Scripts/python.exe -u -m pytest -p no:cacheprovider -q tests > /tmp/full.txt 2>&1
cd frontend && npx tsc --noEmit && npx vitest run src
```

- [ ] **Step 2: 手工核查项追加**

在 `MANUAL-VERIFICATION.md` 末尾（「已知的、清单不覆盖的」之前）追加一节，编号接上现有的 63，并同步更新文件头部的总数和自动化测试计数：

```markdown
## 本体日期类型（`2026-09-10`，7 项）

- [ ] 64. 本体结构页给一个实体类型加一个属性，类型下拉里有「日期」这一项，说明里带例子
- [ ] 65. 传一份 `下单日期` 列写成 `2026/1/15` 的表 → 导入成功，实体明细里显示 `2026-01-15`
- [ ] 66. ⚠️ 同一份表里混进一行 `03/04/2026` → 那一行进报错明细，**其余行照常导入**，
      明细里说得出是「有歧义」不是「格式错误」
- [ ] 67. ⚠️ 前台问「上个月的订单有多少」→ 答得出数字；同一个问题在下个月问，答案跟着变
- [ ] 68. 问「最近 30 天的订单」和「上个月的订单」→ 两个数字不同（一个滚动窗口一个自然月）
- [ ] 69. ⚠️ 把一个图里还有 `2026/1/15` 值的字段改成日期类型并点确认 → **被拒**，
      提示里说得出还有几个、举得出例子；按提示重新导入后再确认 → 通过
- [ ] 70. 引导建模上传带日期列的表 → 审阅页「日期列」那一节说的是能按时间提问，
      并列出认得的写法（不再是「做不了范围过滤」）
```

- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/MANUAL-VERIFICATION.md
git commit -m "docs: 手工清单补上日期类型的 7 项"
```

---

## Self-Review

**Spec coverage**

| Spec 章节 | 落在哪个任务 |
|---|---|
| §3.1 新增 date value_type | Task 3 |
| §3.2 值的不变量 | Global Constraint 1；Task 2、Task 4 落实 |
| §3.3 存储与索引 | Task 3 |
| §4.1 算子表（含不开 starts_with） | Task 5 |
| §4.2 具名区间（12 个 + 边界语义） | Task 1 |
| §4.3 展开 + `today` 注入 | Task 5 |
| §4.4 LLM 工具契约 | Task 6 |
| §5.1 归一化（认/拒的清单） | Task 2 |
| §5.2 接入点（跳过行明细） | Task 4 |
| §6 存量校验（409） | Task 7 |
| §7 UI 三处 | Task 8（**外加 spec 漏掉的 `extraFieldDisplay.ts`**——不改的话下拉里显示的是裸的 `date`） |
| §8 测试 | 各任务的 Step 1 和 Step 5 |
| §9 不做的事 | 无任务，Global Constraints 里体现 |

**Type consistency**：`resolve_period(name, *, today) -> tuple[str, str]`、`normalize_date(raw) -> str`、`is_normalized_date(value) -> bool`、`count_non_iso_date_values(*, tenant_id, term_type, field) -> tuple[int, list[str]]` 在定义处和各调用处同名同型。`PERIOD_NAMES` 是唯一的区间名清单，Task 1 定义，Task 5（报错）和 Task 6（工具说明）都引用它而不是各抄一份。

**执行前的已知不确定项**（实现时要实测确认，不要照抄）：
1. Task 7 的 Cypher 属性名参数化：Neo4j 5 的 `t[$field]` 动态属性访问是否可用；不可用则退回校验后拼接，并在注释里写明字符集已受 `_EXTRA_FIELD_NAME_PATTERN` 限制。
2. Task 5 的 `_SCHEMA_WITH_DATE` fixture 构造方式，照 `tests/graphrag/test_structured_filter_query.py` 里已有的 schema fixture 写。
3. Task 3 的 `tests/graphrag/test_neo4j_client.py` 里 fake driver 的录制手法，照该文件已有用例写。
4. Task 6 的 `_build_prompt` 现有调用点数量——加了 keyword-only 参数之后每一处都要传。
