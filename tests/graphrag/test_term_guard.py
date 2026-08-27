import asyncio

from app.graphrag.ontology import Term
from app.graphrag.term_guard import build_term_guard_context, describe_association

_TERMS = [
    Term(
        tenant_id="t1", node_key="错误码E502",
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
    ),
]


class FakeGraphClient:
    def __init__(self, subgraph_rows: list[dict]) -> None:
        self._rows = subgraph_rows
        self.queried_names: list[str] = []
        self.queried_tenant_ids: list[str] = []

    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        self.queried_names.append(standard_name)
        self.queried_tenant_ids.append(tenant_id)
        return self._rows


async def test_returns_none_when_no_term_matched():
    graph_client = FakeGraphClient(subgraph_rows=[])

    context = await build_term_guard_context(
        "今天天气怎么样", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert context is None
    assert graph_client.queried_names == []


async def test_returns_context_and_queries_graph_when_term_matched():
    graph_client = FakeGraphClient(
        subgraph_rows=[{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    )

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert context is not None
    assert "错误码E502" in context
    assert "登录模块" in context
    assert graph_client.queried_names == ["错误码E502"]
    assert graph_client.queried_tenant_ids == ["t1"]


async def test_marks_two_hop_results_as_indirect_association():
    graph_client = FakeGraphClient(
        subgraph_rows=[
            {"related_name": "登录模块", "relation_type": "RELATED_TO", "hops": 1},
            {"related_name": "会员资格", "relation_type": "REQUIRES", "hops": 2},
        ]
    )

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert "关联: 登录模块" in context
    assert "间接关联（经过 2 跳）: 会员资格" in context


async def test_defaults_to_direct_association_when_hops_field_is_missing():
    """向后兼容：query_subgraph 的 fake/旧实现不带 hops 字段时，仍然按
    直接关联展示，不报错。"""
    graph_client = FakeGraphClient(
        subgraph_rows=[{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    )

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert "关联: 登录模块" in context
    assert "间接关联" not in context


def test_describe_association_labels_direct_and_indirect_hops():
    assert describe_association(1) == "关联"
    assert describe_association(2) == "间接关联（经过 2 跳）"


async def test_caps_injected_neighbors_per_term_and_notes_the_remainder():
    # 2026-08-27 真实案例：某个术语在图谱里挂了 996 个一跳邻居（订单号），
    # 不加上限原样注入直接淹没了 LLM 的上下文，在它开始推理前就把问题
    # 带偏。这里钉住：单个术语最多展示 _MAX_NEIGHBORS_PER_TERM 条，
    # 剩余数量以一句说明收尾，不逐条列出。
    from app.graphrag.term_guard import _MAX_NEIGHBORS_PER_TERM

    many_rows = [
        {"related_name": f"订单{i}", "relation_type": "BELONG_TO", "hops": 1}
        for i in range(_MAX_NEIGHBORS_PER_TERM + 5)
    ]
    graph_client = FakeGraphClient(subgraph_rows=many_rows)

    context = await build_term_guard_context(
        "我这边报了网关超时", terms=_TERMS, tenant_id="t1", graph_client=graph_client
    )

    assert context.count("关联: 订单") == _MAX_NEIGHBORS_PER_TERM
    assert "还有 5 条关联未展示" in context


_TWO_TERMS = [
    Term(
        tenant_id="t1", node_key="错误码E502",
        standard_name="错误码E502", aliases=["网关超时"],
        term_type="error_code",
    ),
    Term(
        tenant_id="t1", node_key="登录模块",
        standard_name="登录模块", aliases=["登录失败"],
        term_type="module",
    ),
]


async def test_build_term_guard_context_queries_multiple_matched_terms_concurrently():
    """命中两个术语时，两次 query_subgraph 调用应该并发发起，不是排队
    顺序执行——用两个互等的 asyncio.Event 证明，退化回顺序执行会卡到
    超时。"""
    started = {"错误码E502": asyncio.Event(), "登录模块": asyncio.Event()}

    class SyncGraphClient:
        async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
            started[standard_name].set()
            other = "登录模块" if standard_name == "错误码E502" else "错误码E502"
            await asyncio.wait_for(started[other].wait(), timeout=5)
            return [{"related_name": f"{standard_name}关联项", "relation_type": "RELATED_TO"}]

    context = await build_term_guard_context(
        "网关超时导致登录失败", terms=_TWO_TERMS, tenant_id="t1",
        graph_client=SyncGraphClient(),
    )

    # 展示顺序必须按 matched（即 terms 表里的原始顺序）排列，不能因为
    # 并发查询导致谁先完成谁排前面。
    assert context.index("错误码E502") < context.index("登录模块")
    assert "错误码E502关联项" in context
    assert "登录模块关联项" in context
