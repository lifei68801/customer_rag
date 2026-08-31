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
    }
