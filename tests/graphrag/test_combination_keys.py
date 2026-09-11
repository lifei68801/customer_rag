import ast
import io
import pathlib

from app.graphrag.ontology_constraints import (
    AllowedCombination,
    CombinationKey,
    to_combination_keys,
)


def test_field_order_is_subject_relation_object():
    """三元组的字段顺序就是 (subject, relation, object)。

    这个顺序是隐式契约：五个构造点各自推导出集合，三个校验点各自拼出待查
    的三元组，两边必须用同一个顺序。写错顺序不会报错——集合只是静默匹配
    不上，表现为"明明配置了这个组合却被判定不在允许列表里"。合并成一个
    函数之后，顺序只写在一处；这条用例把那一处钉住。
    """
    combo = AllowedCombination(
        subject_term_type="订单号", relation_type="SOLD_BY", object_term_type="公司"
    )

    assert to_combination_keys([combo]) == {("订单号", "SOLD_BY", "公司")}


def test_returns_a_set_so_membership_is_constant_time():
    """返回集合而不是列表：调用方全部是在循环里反复做成员判断。"""
    combos = [
        AllowedCombination(subject_term_type="a", relation_type="R", object_term_type="b"),
        AllowedCombination(subject_term_type="c", relation_type="R", object_term_type="d"),
    ]

    keys = to_combination_keys(combos)

    assert isinstance(keys, set)
    assert len(keys) == 2


def test_empty_input_gives_empty_set():
    """空输入返回空集合，不是 None。

    structured_filter_query 用"集合是否为空"来决定要不要跳过整个方向纠正
    逻辑，返回 None 会让那个判断变成 TypeError。
    """
    assert to_combination_keys([]) == set()


def test_duplicates_collapse():
    combo = AllowedCombination(
        subject_term_type="a", relation_type="R", object_term_type="b"
    )

    assert to_combination_keys([combo, combo]) == {("a", "R", "b")}


def test_the_key_carries_field_names():
    """字段能按名字读出来——顺序不再只活在 docstring 里。"""
    key = to_combination_keys(
        [
            AllowedCombination(
                subject_term_type="订单号", relation_type="SOLD_BY", object_term_type="公司"
            )
        ]
    ).pop()

    assert key.subject == "订单号"
    assert key.relation == "SOLD_BY"
    assert key.object == "公司"


def test_a_named_key_still_equals_a_plain_tuple():
    """NamedTuple 跟普通元组相等——所以类型本身挡不住写反的裸元组。

    把这件事写下来，是因为它决定了防线该放在哪：不是类型，而是**构造时
    写出字段名**。下一条用例扫的就是这个。
    """
    key = CombinationKey(subject="a", relation="R", object="b")

    assert key == ("a", "R", "b")
    # 写反的裸元组照样"是一个合法的比较对象"，只是永远匹配不上。
    assert key != ("b", "R", "a")


def test_membership_checks_build_the_key_with_field_names():
    """源码扫描：拿去跟允许集合比对的键，必须用关键字构造。

    这条是这个类型真正的防线。历史上这几处各自拼一个裸三元组，谁把顺序写反
    都不会报错——集合只会静默匹配不上，表现为"明明配置了这个组合却被判定
    不在允许列表里"。而那种退化在任何单元测试里都不会红：写反的那一处只在
    真实数据跑过来的时候才错。

    用 AST 而不是正则：正则分不清 `for c in allowed_combinations`（迭代）和
    `combo in allowed_combinations`（成员判断），也读不了跨行的比较表达式，
    还会把 `declared_types` 这种同前缀的别的变量一起算进来。这三种误判都
    出现过——一个会漏报的检查比没有检查更糟，因为它让人以为这里有防线。
    """
    haystacks = {"allowed_combinations", "allowed_combinations_set", "declared"}
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []

    for path in (repo_root / "app").rglob("*.py"):
        source = io.open(path, encoding="utf-8").read()
        tree = ast.parse(source)

        # 哪些变量名是用 CombinationKey(...) 赋出来的
        keyed_names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "CombinationKey"
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        keyed_names.add(target.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, right in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                if not (isinstance(right, ast.Name) and right.id in haystacks):
                    continue
                left = node.left
                ok = (
                    isinstance(left, ast.Call)
                    and isinstance(left.func, ast.Name)
                    and left.func.id == "CombinationKey"
                ) or (isinstance(left, ast.Name) and left.id in keyed_names)
                if not ok:
                    offenders.append(
                        f"{path.relative_to(repo_root)}:{node.lineno}: "
                        f"{ast.unparse(node)[:80]}"
                    )

    assert not offenders, (
        "这些成员判断没有用 CombinationKey(subject=..., relation=..., object=...) "
        f"构造待查的键，顺序写反不会有任何信号：{offenders}"
    )
