"""value_type 的唯一权威表。

## 为什么要有这个模块

在它出现之前，"一个 value_type 能做什么"被**七处**各自维护着：两份白名单
（ontology_categories / ontology_lifecycle）、两个类型闸（terms_store 的
`_extra_property_value_matches_type` / `_coerce_to_declared_type`）、两条转换
链（schema_etl_row_processing 的 `convert_field_value` / schema_etl_sample 的
`_example_values_for`）、一份可建索引集合（neo4j_client 的
`_SCALAR_VALUE_TYPES`）。

2026-09-10 加 `date` 类型时漏改了其中四处，而**四处的单元测试全是绿的**
——每个模块只测自己。抓住其中三处的是一条跨模块的端到端测试；
`_coerce_to_declared_type` 那一处连端到端都没抓住，因为它根本不在 ETL 链路
上。见 `docs/superpowers/specs/2026-09-10-ontology-date-type-design.md` §3.1。

这七处里有六处是"不认识就落到最后一条兜底分支"的写法：漏掉的分支不会
提示"这里还需要加一个类型"，只会静默判否、或抛一句笼统的"还不支持"。

现在它们都查这张表。加一个类型 = 在 `VALUE_TYPES` 里加一行。

## 两条转换路径是有意不同的

`from_source_text`（ETL 读源表）和 `from_admin_text`（管理员在界面上填）
对同一个类型可以有不同行为，这是设计决定，不是不一致：

- `date`：ETL 归一化（`2026/1/5` → `2026-01-05`），因为源表的写法五花八门
  而数据是批量的；界面拒绝并要求管理员改成补零格式，因为"输入 A 存进去 B"
  会让人摸不着头脑。
- `number`：ETL 保留 `float`；界面把 `42.0` 收成 `42`，否则下次 ETL 写
  `42`（int）时 `42.0 != 42` 又记一条冲突。
- `number[]`：界面上没有填它的入口，`from_admin_text` 是 None。

把这两条路径并排放进同一行，是为了让这种非对称看得见——散在两个模块里时
它看起来只是"某一处忘了同步"。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.graphrag.date_normalization import (
    InvalidDateValueError,
    is_normalized_date,
    normalize_date,
)


class UnknownValueTypeError(ValueError):
    """这个 value_type 不在表里。"""


class ValueTypeConversionError(ValueError):
    """值转不成这个 value_type 声明的类型。

    消息是**原因**（"值 'abc' 不是一个 integer"），不带字段名——字段名由
    调用方拼在外面，因为只有调用方知道该叫"字段"还是"列"。
    """


@dataclass(frozen=True)
class ValueTypeSpec:
    """一个 value_type 的全部能力。字段顺序即这张表的列顺序。"""

    name: str

    #: 能不能当 term_type 自身的取值类型（standard_name）。
    #: `date` 和 `number[]` 不能：standard_name 是实体的名字，一个日期或
    #: 一串数字不该当实体的名字。这条非对称是有意的，不是漏了。
    allowed_for_standard_name: bool

    #: 在 Neo4j 里能不能建索引。标量可以；`number[]` 不行（数组属性上的
    #: 复合索引不服务范围过滤）。`date` 在图里是字符串属性、字典序即时间序，
    #: 范围过滤全指着这个索引，所以必须是 True。
    graph_indexable: bool

    #: 「下载 ETL 示例文件」里这一列给的两行示例值。
    example_values: tuple[str, str]

    #: 写库前的类型闸：这个 Python 值符不符合声明的类型。
    matches: Callable[[object], bool]

    #: ETL 路径：把源表读出来的字符串转成落库的值。
    #: 转不动时抛 ValueTypeConversionError。
    from_source_text: Callable[[str], object]

    #: 界面路径：把管理员填的字符串转成落库的值。
    #: None 表示界面上没有填这个类型的入口。
    from_admin_text: Callable[[str], object] | None


def _matches_number(value: object) -> bool:
    # bool 是 int 的子类，isinstance(True, int) 为真——不排除的话 True 会被
    # 当成合法的 number 写进图里。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _matches_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _matches_number_array(value: object) -> bool:
    return isinstance(value, list) and all(_matches_number(v) for v in value)


def _matches_date(value: object) -> bool:
    # 判的是"已经是补零 ISO"，不是"能不能归一"——这是写库前的最后一道闸，
    # 放行一个没补零的值就等于把破坏字典序的数据放进图里（见
    # date_normalization 的模块文档）。
    return isinstance(value, str) and is_normalized_date(value)


def _parse_int(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueTypeConversionError(f"值 {raw!r} 不是一个 integer") from None


def _parse_float(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ValueTypeConversionError(f"值 {raw!r} 不是一个 number") from None


def _parse_number_array(raw: str) -> list[float]:
    # 分号分隔，跟「下载 ETL 示例文件」给的示例值对齐。
    return [_parse_float(item) for item in raw.split(";") if item.strip()]


def _admin_number(raw: str) -> int | float:
    parsed = _parse_float(raw)
    # 42.0 存成 42：整数值存成浮点的话，下次 ETL 写 42（int）时
    # 42.0 != 42 又是一条冲突，而页面上两边 str() 之后都显示 42，
    # 审核员看到「用 42 / 用 42」，怎么点都消不掉。
    return int(parsed) if parsed.is_integer() else parsed


def _source_date(raw: str) -> str:
    try:
        return normalize_date(raw)
    except InvalidDateValueError as exc:
        raise ValueTypeConversionError(str(exc)) from None


def _admin_date(raw: str) -> str:
    if is_normalized_date(raw):
        return raw
    raise ValueTypeConversionError(
        f"{raw!r} 不是补零的 YYYY-MM-DD 格式，例如 2026-01-05。"
        "请改成这个格式再提交。"
    )


#: 唯一的一张表。加一个 value_type 就是在这里加一行。
VALUE_TYPES: dict[str, ValueTypeSpec] = {
    spec.name: spec
    for spec in (
        ValueTypeSpec(
            name="string",
            allowed_for_standard_name=True,
            graph_indexable=True,
            example_values=("示例文本1", "示例文本2"),
            matches=lambda v: isinstance(v, str),
            from_source_text=lambda raw: raw,
            from_admin_text=lambda raw: raw,
        ),
        ValueTypeSpec(
            name="number",
            allowed_for_standard_name=True,
            graph_indexable=True,
            example_values=("1.5", "2.5"),
            matches=_matches_number,
            from_source_text=_parse_float,
            from_admin_text=_admin_number,
        ),
        ValueTypeSpec(
            name="integer",
            allowed_for_standard_name=True,
            graph_indexable=True,
            example_values=("1", "2"),
            matches=_matches_integer,
            from_source_text=_parse_int,
            from_admin_text=_parse_int,
        ),
        ValueTypeSpec(
            name="number[]",
            allowed_for_standard_name=False,
            graph_indexable=False,
            example_values=("1.5;2.5", "3.5;4.5"),
            matches=_matches_number_array,
            from_source_text=_parse_number_array,
            # 界面上还没有填它的入口。给 None 而不是猜一个解析方式：猜错了
            # 这个实体的那一列从此是脏的，而界面上看起来完全正常。
            from_admin_text=None,
        ),
        ValueTypeSpec(
            name="date",
            allowed_for_standard_name=False,
            graph_indexable=True,
            # 两个不同的补零 ISO 日期，而不是同一天写两次——同一份示例文件里
            # 两行的其他字段本来就是递增的两个不同示例。
            example_values=("2026-01-05", "2026-02-10"),
            matches=_matches_date,
            from_source_text=_source_date,
            from_admin_text=_admin_date,
        ),
    )
}

#: extra_field 能声明的类型。
EXTRA_FIELD_VALUE_TYPES = frozenset(VALUE_TYPES)

#: term_type 自身取值（standard_name）能声明的类型。比上面那张窄。
STANDARD_NAME_VALUE_TYPES = frozenset(
    name for name, spec in VALUE_TYPES.items() if spec.allowed_for_standard_name
)

#: 能在 Neo4j 上建索引的类型。
GRAPH_INDEXABLE_VALUE_TYPES = frozenset(
    name for name, spec in VALUE_TYPES.items() if spec.graph_indexable
)


def spec_for(value_type: str) -> ValueTypeSpec:
    """取这个类型的那一行，不认识就抛——不返回 None。

    返回 None 会让调用方写出 `if spec is None: pass` 这种静默跳过，而"静默
    跳过一个没见过的类型"正是这张表要消灭的那类缺陷。
    """
    try:
        return VALUE_TYPES[value_type]
    except KeyError:
        raise UnknownValueTypeError(
            f"未知的 value_type: {value_type!r}，仅支持: {sorted(VALUE_TYPES)}"
        ) from None


def value_matches_type(value: object, value_type: str) -> bool:
    """写库前的类型闸。不认识的类型判否——这一步是防线，不是分发。"""
    if value_type not in VALUE_TYPES:
        return False
    return VALUE_TYPES[value_type].matches(value)


def convert_source_text(value_type: str, raw: str) -> object:
    """ETL 路径：源表里的字符串 → 落库的值。

    UnknownValueTypeError / ValueTypeConversionError 都是 ValueError 的子类，
    调用方想合并处理就 except ValueError。
    """
    return spec_for(value_type).from_source_text(raw)


def convert_admin_text(value_type: str, raw: str) -> object:
    """界面路径：管理员填的字符串 → 落库的值。"""
    converter = spec_for(value_type).from_admin_text
    if converter is None:
        raise ValueTypeConversionError(
            f"声明的类型是 {value_type}，这个类型还不支持在界面上直接改。"
        )
    return converter(raw)


def example_values_for(value_type: str) -> tuple[str, str]:
    """「下载 ETL 示例文件」里这一列的两行示例值。"""
    return spec_for(value_type).example_values
