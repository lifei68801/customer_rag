"""value_type 权威表的测试。

这张表存在的理由是：加 `date` 那次，七个各自维护类型枚举的地方漏改了四处，
而四处的单元测试全是绿的。所以这里的测试刻意写成**表驱动**——对每一个
value_type 都跑一遍同一组问题，新加一个类型时漏掉哪个能力，这里立刻红。
"""

import pytest

from app.graphrag.value_types import (
    EXTRA_FIELD_VALUE_TYPES,
    GRAPH_INDEXABLE_VALUE_TYPES,
    STANDARD_NAME_VALUE_TYPES,
    VALUE_TYPES,
    UnknownValueTypeError,
    ValueTypeConversionError,
    convert_admin_text,
    convert_source_text,
    example_values_for,
    spec_for,
    value_matches_type,
)

ALL_TYPES = sorted(VALUE_TYPES)


@pytest.mark.parametrize("value_type", ALL_TYPES)
def test_every_value_type_answers_every_question(value_type):
    """每个类型都得把整行填满。

    这是防"加了新类型只填一半"的那条闸：新类型漏了示例值、漏了匹配函数、
    漏了 ETL 转换，都会在这里红，而不是等到某个租户下载示例文件时 500。
    """
    spec = spec_for(value_type)
    assert isinstance(spec.allowed_for_standard_name, bool)
    assert isinstance(spec.graph_indexable, bool)
    assert len(spec.example_values) == 2
    assert all(isinstance(v, str) and v for v in spec.example_values)
    assert callable(spec.matches)
    assert callable(spec.from_source_text)
    # from_admin_text 可以是 None（界面上没有入口），但不能是别的东西。
    assert spec.from_admin_text is None or callable(spec.from_admin_text)


@pytest.mark.parametrize("value_type", ALL_TYPES)
def test_example_values_survive_the_etl_conversion(value_type):
    """示例文件里给的值，必须是 ETL 真的能读进去的值。

    这两处以前分在两个模块里各写一份 if/elif，谁也不保证对得上——给出一份
    ETL 自己都读不了的示例文件，用户照着填完才发现导入失败，而错误信息指向
    的是他自己的数据。
    """
    spec = spec_for(value_type)
    for example in spec.example_values:
        converted = convert_source_text(value_type, example)
        # 转换出来的值还要过得了写库前那道类型闸，否则示例数据会在最后一步
        # 被判类型不匹配。
        assert value_matches_type(converted, value_type), (
            f"{value_type} 的示例值 {example!r} 转换后是 {converted!r}，"
            "过不了类型闸"
        )


def test_standard_name_types_are_a_strict_subset():
    """两张白名单的非对称是有意的，这里把它钉住。

    date 和 number[] 不能当 standard_name：standard_name 是实体的名字。
    以后有人"顺手"把它们加进去时，这条会红。
    """
    assert STANDARD_NAME_VALUE_TYPES < EXTRA_FIELD_VALUE_TYPES
    assert STANDARD_NAME_VALUE_TYPES == {"string", "number", "integer"}


def test_date_is_graph_indexable():
    """date 必须可建索引。

    图里的 date 是字符串属性、字典序即时间序，范围过滤全指着这个索引。
    漏掉它功能照样对，只是每次全表扫——压测之前谁也看不出来。
    """
    assert "date" in GRAPH_INDEXABLE_VALUE_TYPES
    # number[] 不建索引：数组属性上的复合索引不服务范围过滤。
    assert "number[]" not in GRAPH_INDEXABLE_VALUE_TYPES


@pytest.mark.parametrize(
    ("value_type", "value", "expected"),
    [
        ("string", "abc", True),
        ("string", 1, False),
        ("number", 1.5, True),
        ("number", 1, True),
        ("number", True, False),
        ("integer", 3, True),
        ("integer", 3.5, False),
        ("integer", True, False),
        ("number[]", [1.0, 2.0], True),
        ("number[]", [1.0, "x"], False),
        ("number[]", 1.0, False),
        ("date", "2026-01-05", True),
        ("date", "2026-1-5", False),
        ("date", 20260105, False),
    ],
)
def test_type_gate(value_type, value, expected):
    assert value_matches_type(value, value_type) is expected


def test_bool_is_never_a_number():
    """bool 是 int 的子类，不显式排除就会被当成合法的 number 写进图里。"""
    assert value_matches_type(True, "number") is False
    assert value_matches_type(False, "integer") is False


def test_unknown_type_fails_the_gate_instead_of_passing():
    """没见过的类型判否，不是放行。

    这一步是写库前的最后一道防线；"不认识就放行"等于没有防线。
    """
    assert value_matches_type("whatever", "geopoint") is False


def test_unknown_type_is_named_in_the_error():
    """报错要说出支持哪些，否则调用方不知道该改成什么。"""
    with pytest.raises(UnknownValueTypeError) as exc:
        spec_for("geopoint")
    assert "geopoint" in str(exc.value)
    for name in ALL_TYPES:
        assert name in str(exc.value)


def test_source_date_is_normalized_but_admin_date_is_refused():
    """两条转换路径对 date 的行为不同，这是设计决定，不是不一致。

    ETL 归一化（源表写法五花八门、数据是批量的）；界面拒绝并要求管理员自己
    改成补零格式（"输入 A 存进去 B"会让人摸不着头脑）。
    """
    assert convert_source_text("date", "2026/1/5") == "2026-01-05"
    with pytest.raises(ValueTypeConversionError) as exc:
        convert_admin_text("date", "2026/1/5")
    # 报错里要带正确格式的例子，让管理员照着改。
    assert "2026-01-05" in str(exc.value)


def test_admin_number_collapses_integral_floats_but_source_does_not():
    """界面把 42.0 收成 42，ETL 保留 float。

    不收的话，下次 ETL 写 42（int）时 42.0 != 42 又记一条冲突，而页面上两边
    str() 之后都显示 42：审核员看到「用 42 / 用 42」，怎么点都消不掉。
    """
    assert convert_admin_text("number", "42.0") == 42
    assert isinstance(convert_admin_text("number", "42.0"), int)
    assert convert_source_text("number", "42.0") == 42.0
    assert isinstance(convert_source_text("number", "42.0"), float)


def test_number_array_has_no_admin_entry_and_says_so():
    """界面上没有填 number[] 的入口——明确拒绝，不猜一个解析方式。

    猜错了这个实体的那一列从此是脏的，而界面上看起来完全正常。
    """
    assert spec_for("number[]").from_admin_text is None
    with pytest.raises(ValueTypeConversionError) as exc:
        convert_admin_text("number[]", "1.5;2.5")
    assert "number[]" in str(exc.value)


def test_conversion_error_says_why_not_just_that_it_failed():
    with pytest.raises(ValueTypeConversionError) as exc:
        convert_source_text("integer", "abc")
    assert "abc" in str(exc.value) and "integer" in str(exc.value)


def test_example_values_for_unknown_type_raises():
    with pytest.raises(UnknownValueTypeError):
        example_values_for("geopoint")


def test_no_module_keeps_its_own_copy_of_the_value_type_whitelist():
    """源码扫描：不许再出现第二份类型枚举。

    这条是这张表的看门狗。七处各自维护同一份枚举正是它要消灭的形状，而
    "又抄了一份"在任何单元测试里都不会红——抄的那份一开始总是对的，它要
    等到下一次加类型时才变错。

    扫的是字面量组合而不是变量名：改个变量名就能绕过的检查等于没有检查。
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    this_module = repo_root / "app" / "graphrag" / "value_types.py"
    offenders = []
    for path in (repo_root / "app").rglob("*.py"):
        if path == this_module:
            continue
        source = path.read_text(encoding="utf-8")
        # 一行里同时出现 "integer" 和 "number[]" 两个字面量，基本只可能是
        # 又抄了一份枚举。表里这两个类型一个是标量一个是数组，没有别的
        # 理由让它们并排出现。
        for lineno, line in enumerate(source.splitlines(), 1):
            if '"integer"' in line and '"number[]"' in line:
                offenders.append(f"{path.relative_to(repo_root)}:{lineno}")
    assert not offenders, (
        "这些地方看起来又维护了一份 value_type 枚举，应该改成从 "
        f"app.graphrag.value_types 导入：{offenders}"
    )


def test_the_frontend_copy_of_the_table_matches_this_one():
    """前端硬编码的那份 value_type 列表必须跟这张表一致。

    浏览器读不到 Python，所以 `frontend/src/admin/ontologySchema/shared.ts`
    里有一份手抄的副本。加一个类型时漏改它不会有任何信号：下拉框里少一个
    选项，没有人会因此变红——正是这张表当初要消灭的那种缺陷，只是换到了
    语言边界上。

    真正消掉它要让后端把这张表作为接口暴露出去、前端拉取而不是硬编码。
    在那之前，这条跨语言的对照就是唯一的防线。
    """
    import pathlib
    import re

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    shared = repo_root / "frontend" / "src" / "admin" / "ontologySchema" / "shared.ts"
    source = shared.read_text(encoding="utf-8")

    def listed(const_name: str) -> list[str]:
        match = re.search(rf"export const {const_name} = \[(.*?)\] as const", source, re.S)
        assert match, f"{shared.name} 里找不到 {const_name}——它是不是改名或搬走了？"
        return re.findall(r"'([^']+)'", match.group(1))

    assert sorted(listed("VALUE_TYPES")) == sorted(EXTRA_FIELD_VALUE_TYPES), (
        "前端的 VALUE_TYPES 跟后端这张表对不上。加类型时两边都要改，"
        "或者给它加一个接口让前端去拉。"
    )
    assert sorted(listed("STANDARD_NAME_VALUE_TYPES")) == sorted(STANDARD_NAME_VALUE_TYPES), (
        "前端的 STANDARD_NAME_VALUE_TYPES 跟后端对不上。"
    )
