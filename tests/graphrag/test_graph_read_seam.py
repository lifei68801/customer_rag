"""图谱读接缝的测试。

这个接缝是项目里唯一一个有两个真适配器的图谱接缝——其余五个协议各自只有
一个适配器，也就是说它们保护的是一个不存在的变化点。这里的测试钉住"两个
适配器都真的实现了它"，因为一旦其中一个退化成存根，这个接缝就跟其余五个
一样成了摆设，而那件事不会有任何别的测试变红。
"""

import ast
import inspect
import pathlib
import re

from app.graphrag.graph_read import GraphReadProtocol
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.neptune_client import NeptuneGraphClient

ADAPTERS = (Neo4jGraphClient, NeptuneGraphClient)

PROTOCOL_METHODS = tuple(
    name for name in vars(GraphReadProtocol) if not name.startswith("_")
)


def _is_stub(cls, method_name: str) -> bool:
    """这个方法体是不是只有一句 raise NotImplementedError。"""
    source = inspect.getsource(getattr(cls, method_name))
    tree = ast.parse(inspect.cleandoc(source))
    func = tree.body[0]
    body = [
        node
        for node in func.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Raise)
        and "NotImplementedError" in ast.dump(body[0])
    )


def test_the_protocol_is_not_empty():
    """协议里至少要有方法——空协议任何类都"满足"，下面两条会假绿。"""
    assert PROTOCOL_METHODS


def test_both_adapters_really_implement_the_read_seam():
    """两个适配器都真的实现，不是存根。

    「一个适配器是假接缝，两个才是真的」——这条测试就是在守这句话。
    NeptuneGraphClient 上另有 15 个显式的 NotImplementedError 存根；只要
    读接缝的方法掉进那一批，这里立刻红。
    """
    for adapter in ADAPTERS:
        for method in PROTOCOL_METHODS:
            assert hasattr(adapter, method), f"{adapter.__name__} 缺少 {method}"
            assert not _is_stub(adapter, method), (
                f"{adapter.__name__}.{method} 退化成了 NotImplementedError 存根——"
                "读接缝就只剩一个真适配器了"
            )


def test_stub_error_messages_point_at_a_document_that_exists():
    """存根的报错里给的文档路径必须真的存在。

    这十五条消息曾经整齐地指着 docs/superpowers/plans/ 下一个从未存在过的
    文件（真正的文档在 specs/ 下，叫另一个名字）。一条把人引到死路径的
    报错比不给路径更糟：读的人会以为是自己 checkout 缺了东西。
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    source = (repo_root / "app" / "graphrag" / "neptune_client.py").read_text(
        encoding="utf-8"
    )
    # 用正则取路径：消息和注释里路径后面可能紧跟中文（"……的 '未决风险' 一节"），
    # 按引号或空格切会把那些字一起算进路径。
    referenced = set(re.findall(r"docs/[\w./-]+\.md", source))
    missing = [d for d in referenced if not (repo_root / d).exists()]
    assert not missing, f"这些文档路径不存在：{sorted(missing)}"
