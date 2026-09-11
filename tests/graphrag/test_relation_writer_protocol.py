import pytest

from app.graphrag.normalization import GraphWriteClientProtocol
from app.graphrag.relation_writer import RelationWriterProtocol
from app.graphrag.review_queue import ReviewGraphClientProtocol
from app.graphrag.schema_etl import SchemaEtlGraphProtocol

_ALL = [
    RelationWriterProtocol,
    GraphWriteClientProtocol,
    ReviewGraphClientProtocol,
    SchemaEtlGraphProtocol,
]


@pytest.mark.parametrize("proto", _ALL, ids=lambda p: p.__name__)
def test_stays_a_structural_protocol(proto):
    """继承基协议之后仍然是结构化协议，不是普通基类。

    子类必须同时把 Protocol 列进基类（`class X(Base, Protocol)`）；只写
    `class X(Base)` 会静默退化成一个普通类——此时 Neo4jGraphClient 这种
    "只是碰巧有同名方法、并不继承任何协议"的实现就不再满足它了。这个项目
    没有类型检查（pyproject.toml 里无 mypy/pyright，CI 只跑 pytest），
    退化不会有任何其它信号，所以在这里显式钉住。
    """
    assert proto._is_protocol is True


@pytest.mark.parametrize("proto", _ALL, ids=lambda p: p.__name__)
def test_declares_merge_relation(proto):
    """四个协议都必须声明 merge_relation——三个子协议靠继承拿到它。

    这条会在"忘了继承基协议、又把签名删了"时变红。
    """
    assert "merge_relation" in proto.__protocol_attrs__


def test_each_consumer_protocol_keeps_only_what_it_uses():
    """2026-08-27 那次拆分的意图是"每个消费方只看到自己真正调用的方法"。
    抽出公共基协议是为了让签名只写一遍，不是为了把方法集合并起来——这条
    用例防止有人顺手把三个集合合成一个大协议。
    """
    assert ReviewGraphClientProtocol.__protocol_attrs__ == {"merge_relation"}
    assert GraphWriteClientProtocol.__protocol_attrs__ == {
        "merge_relation",
        "delete_relations_by_source",
    }
    assert SchemaEtlGraphProtocol.__protocol_attrs__ == {
        "merge_relation",
        "sync_term",
        "delete_term_node",
        "delete_stale_relations_by_source",
        "count_stale_relations_by_source",
    }


def test_merge_relation_takes_node_keys_not_display_names():
    """参数名必须说出它收的是什么。

    这两个参数曾经叫 subject_standard_name/object_standard_name，而收的值
    一直是 node_key——接口的名字跟 ADR-0003 直接矛盾。照着名字传展示名不会
    有任何报错（这个项目不跑类型检查），只会在术语改名之后断边：用旧的
    展示名去 MERGE，命中不了真实节点，于是新建一个没有 standard_name 属性
    的幽灵节点。

    名字退回去不会有任何别的测试变红——退回去的那一刻代码还是对的，代价要
    等到下一个照着名字写调用的人才付。所以在这里钉住。
    """
    import inspect

    from app.graphrag.neo4j_client import Neo4jGraphClient

    params = inspect.signature(Neo4jGraphClient.merge_relation).parameters
    assert "subject_node_key" in params
    assert "object_node_key" in params
    assert "subject_standard_name" not in params
    assert "object_standard_name" not in params

    # 四个协议声明的也得是同一套名字，否则实现和协议对不上而没人发现。
    for proto in _ALL:
        proto_params = inspect.signature(proto.merge_relation).parameters
        assert "subject_node_key" in proto_params, proto.__name__
        assert "subject_standard_name" not in proto_params, proto.__name__
