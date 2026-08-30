from app.graphrag.ontology_constraints import AllowedCombination, to_combination_keys


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
