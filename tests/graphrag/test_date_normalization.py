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
    """Excel 序列号不认：序列号本身在数据上跟一个普通整数无法区分，认它
    就是在猜。（未验证：convert_excel_cell_to_string 是否总是把日期单元格
    转成统一的 %Y-%m-%d——实际它对零点整的 datetime 转成 %Y-%m-%d，对带
    时间的转成 %Y-%m-%d %H:%M:%S，两种 normalize_date 都会拒，但不能据此
    断言序列号只在未格式化时才会出现。）"""
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


def test_two_digit_year_is_not_a_known_spelling_and_is_refused():
    """两位年份是 2026 还是 1926 说不清，但它走的不是「有歧义」那条专门
    路径——`_DAY_OR_MONTH_FIRST` 要求年份是结尾的 4 位数，`"26-1-5"` 最后
    一段是 "5"，不匹配，所以落进通用「认不出」分支，不含「歧义」字样。

    这是变异 D 的靶子用例：`_YEAR_FIRST` 放宽成 `(\\d{1,4})` 后，
    `date(26, 1, 5)` 会被静默接受成公元 26 年而不报错。
    """
    with pytest.raises(InvalidDateValueError) as exc:
        normalize_date("26-1-5")
    assert "歧义" not in str(exc.value)
