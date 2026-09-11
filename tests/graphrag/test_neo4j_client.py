import logging
from datetime import datetime

import pytest

from app.graphrag.neo4j_client import UNKNOWN_COUNTERPART_TYPE, Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.structured_filter_query import AttributeConstraint, ExpandSpec, ResolvedAnchor, TypeAnchor

_NOW = datetime(2026, 8, 12, 12, 0, 0)

# 链式查询资格来自租户本体配置（tenant_relation_types.allow_chain_query），
# 不是写死的那三种。这份测试数据刻意避开 REQUIRES/PRECEDES/PART_OF：用默认
# 那三种的话，"按传入集合动态拼接"和"仍然硬编码"两种实现都能让断言变绿。
_CHAIN_TYPES = {"DEPENDS_ON", "FOLLOWS"}


class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def data(self) -> list[dict]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[dict] | None = None, *, call_results: list | None = None) -> None:
        """rows：不管调几次 .run()，每次都返回这同一份数据（绝大多数现有测试的用法，
        不用改）。call_results：按 .run() 调用顺序消费的结果列表，每个元素是
        list[dict]（多行）或 dict（单行，会被包成 [dict]）——两个参数二选一。"""
        self._rows = rows if rows is not None else []
        self._call_results = call_results
        self._call_index = 0
        self.last_query: str | None = None
        self.last_parameters: dict | None = None
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, parameters: dict | None = None) -> FakeResult:
        self.last_query = query
        self.last_parameters = parameters
        self.calls.append((query, parameters))
        if self._call_results is not None:
            result = self._call_results[self._call_index]
            self._call_index += 1
            return FakeResult(result if isinstance(result, list) else [result])
        return FakeResult(self._rows)

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeDriver:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def session(self) -> FakeSession:
        return self._session


async def test_query_subgraph_returns_related_terms():
    session = FakeSession(
        rows=[
            {"related_name": "登录模块", "relation_type": "RELATED_TO"},
        ]
    )
    client = Neo4jGraphClient(driver=FakeDriver(session))

    results = await client.query_subgraph("错误码E502", tenant_id="t1", chain_query_relation_types=_CHAIN_TYPES)

    assert results == [{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    assert session.last_parameters == {"node_key": "错误码E502", "tenant_id": "t1"}
    assert "WHERE r.tenant_id = $tenant_id" in session.last_query


async def test_merge_relation_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_node_key="错误码E502",
        object_node_key="登录模块",
        relation_type="RELATED_TO",
        source="a.md",
        tenant_id="t1",
        provenance="auto_merged",
        recorded_at=_NOW,
    )

    assert session.last_parameters == {
        "subject_name": "错误码E502",
        "object_name": "登录模块",
        "source": "a.md",
        "tenant_id": "t1",
        "provenance": "auto_merged",
        "recorded_at": "2026-08-12 12:00:00",
    }
    assert "RELATED_TO" in session.last_query
    assert "MERGE" in session.last_query
    assert "tenant_id" in session.last_query
    # tenant_id 必须在 MERGE 的匹配模式本身里（不能只在匹配到之后才 SET），
    # 否则两个租户各自抽取出同一对标准术语间的同类型关系时，后写入的会
    # 命中并覆盖先写入的那条边——见 merge_relation 的说明。
    assert "MERGE (a)-[r:RELATED_TO {tenant_id: $tenant_id}]->(b)" in session.last_query


async def test_delete_relations_by_source_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_relations_by_source("a.md", tenant_id="t1")

    assert session.last_parameters == {"source": "a.md", "tenant_id": "t1"}
    assert "DELETE" in session.last_query
    assert "tenant_id" in session.last_query


async def test_merge_relation_rejects_unrecognized_relation_type():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    try:
        await client.merge_relation(
            subject_node_key="a",
            object_node_key="b",
            relation_type="DROP TABLE",
            source="a.md",
            tenant_id="t1",
            provenance="auto_merged",
            recorded_at=_NOW,
        )
        assert False, "应拒绝非法关系类型"
    except ValueError:
        pass


async def test_merge_relation_rejects_alias_of():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    try:
        await client.merge_relation(
            subject_node_key="a",
            object_node_key="b",
            relation_type="ALIAS_OF",
            source="a.md",
            tenant_id="t1",
            provenance="auto_merged",
            recorded_at=_NOW,
        )
        assert False, "应拒绝 ALIAS_OF：该关系类型只能由 sync_term 写入，不设置 tenant_id"
    except ValueError:
        pass


async def test_sync_term_writes_standard_node_properties_and_alias_edges():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        tenant_id="t1", node_key="错误码E502", standard_name="错误码E502",
        aliases=["网关超时", "E502超时"],
        term_type="error_code",
    )

    await client.sync_term(term)

    assert session.last_parameters == {
        "tenant_id": "t1",
        "node_key": "错误码E502",
        "standard_name": "错误码E502",
        "type": "error_code",
        "aliases": ["网关超时", "E502超时"],
        "extra_properties": {},
    }
    assert "ALIAS_OF" in session.last_query
    assert "alias_name" in session.last_query


async def test_sync_term_with_no_aliases_sends_empty_alias_list():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        tenant_id="t1", node_key="登录模块", standard_name="登录模块",
        aliases=[],
        term_type="module",
    )

    await client.sync_term(term)

    assert session.last_parameters["aliases"] == []


async def test_sync_term_writes_extra_properties():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        tenant_id="t1", node_key="错误码E502",
        standard_name="错误码E502", aliases=[], term_type="错误码",
        extra_properties={"严重等级": "高"},
    )

    await client.sync_term(term)

    assert session.last_parameters["extra_properties"] == {"严重等级": "高"}
    assert "SET t += $extra_properties" in session.last_query


async def test_sync_terms_syncs_every_term_in_the_list():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    terms = [
        Term(
            tenant_id="t1", node_key="错误码E502", standard_name="错误码E502",
            aliases=["网关超时"], term_type="error_code",
        ),
        Term(
            tenant_id="t1", node_key="登录模块", standard_name="登录模块",
            aliases=["认证模块"], term_type="module",
        ),
    ]

    await client.sync_terms(terms)

    assert len(session.calls) == 2
    synced_names = {call[1]["standard_name"] for call in session.calls}
    assert synced_names == {"错误码E502", "登录模块"}


async def test_merge_relation_accepts_new_part_of_type():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_node_key="大床房",
        object_node_key="酒店",
        relation_type="PART_OF",
        source="a.md",
        tenant_id="t1",
        provenance="auto_merged",
        recorded_at=_NOW,
    )

    assert "PART_OF" in session.last_query


async def test_merge_relation_accepts_tenant_defined_type_not_in_old_whitelist():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_node_key="Product:1001",
        object_node_key="SKU:4901234567890",
        relation_type="HAS_SKU",
        source="skus.csv",
        tenant_id="muji",
        provenance="etl",
        recorded_at=_NOW,
    )

    assert "HAS_SKU" in session.last_query


async def test_query_subgraph_sends_two_hop_union_query_for_chain_relations():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("错误码E502", tenant_id="t1", chain_query_relation_types=_CHAIN_TYPES)

    assert "UNION" in session.last_query
    # 关系类型按传入集合排序后拼接，不再是写死的 REQUIRES|PRECEDES|PART_OF。
    assert "[r:DEPENDS_ON|FOLLOWS*2..2]" in session.last_query
    assert "REQUIRES" not in session.last_query
    assert "PART_OF" not in session.last_query
    assert "ALL(rel IN r WHERE rel.tenant_id = $tenant_id)" in session.last_query
    assert "AND related <> t" in session.last_query
    assert session.last_parameters == {"node_key": "错误码E502", "tenant_id": "t1"}


def _subgraph_union_branches(query: str) -> tuple[str, str]:
    """把 _SUBGRAPH_QUERY 拆成 UNION 前后两段。

    下面几条断言必须钉住"这一段里有租户过滤"，而不是"整条语句某处出现过"
    ——两段各自匹配对端节点，只补其中一段的话，另一段照样会把别的租户的
    节点返回给 LLM，而一条整体查找的断言仍然是绿的。
    """
    branches = query.split("UNION")
    assert len(branches) == 2, f"期望 _SUBGRAPH_QUERY 恰好有两段 UNION：{query}"
    return branches[0], branches[1]


async def test_list_term_relations_scopes_related_node_by_tenant():
    """详情页的关系列表里，对端节点也必须按租户过滤。

    起点节点和边都过滤了、唯独对端节点没有的话，一条"边标着本租户、对端
    节点属别的租户"的边会把别人的 standard_name 列进管理界面。这类边正常
    写入路径产生不了（merge_relation 给两端节点和边用同一个 $tenant_id），
    但早期示例数据里出现过租户标记不一致的历史脏边——查询不该依赖数据干净。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.list_term_relations(tenant_id="t1", node_key="k1")

    assert (
        "MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})"
        "-[r]-(related:Term {tenant_id: $tenant_id})"
    ) in session.last_query
    assert session.last_parameters == {"tenant_id": "t1", "node_key": "k1"}


async def test_query_subgraph_one_hop_branch_scopes_related_node_by_tenant():
    """检索上下文的 1 跳分支：对端节点按租户过滤。泄漏点比详情页更严重
    ——这里返回的 standard_name 会直接进 LLM 的回答。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("k1", tenant_id="t1", chain_query_relation_types=_CHAIN_TYPES)

    one_hop, _ = _subgraph_union_branches(session.last_query)
    assert "-[r]-(related:Term {tenant_id: $tenant_id})" in one_hop


async def test_query_subgraph_two_hop_branch_scopes_related_node_by_tenant():
    """检索上下文的 2 跳分支：终点节点按租户过滤。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("k1", tenant_id="t1", chain_query_relation_types=_CHAIN_TYPES)

    _, two_hop = _subgraph_union_branches(session.last_query)
    assert "(related:Term {tenant_id: $tenant_id})" in two_hop


async def test_query_subgraph_two_hop_branch_scopes_intermediate_nodes_by_tenant():
    """2 跳分支里还有一个中间节点，它同样要按租户校验。

    ALL(rel IN r WHERE ...) 只管住了路径上的每条边，给终点节点补上
    {tenant_id: $tenant_id} 也只管住了两头——中间那个节点仍然可以是别的
    租户的。校验必须覆盖路径上的全部节点（nodes(p)），只钉终点的实现要红。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("k1", tenant_id="t1", chain_query_relation_types=_CHAIN_TYPES)

    _, two_hop = _subgraph_union_branches(session.last_query)
    assert "MATCH p = (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in two_hop
    assert "ALL(n IN nodes(p) WHERE n.tenant_id = $tenant_id)" in two_hop


async def test_rename_term_node_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.rename_term_node(
        tenant_id="t1", node_key="错误码E502", new_standard_name="错误码E502v2"
    )

    assert session.last_parameters == {
        "tenant_id": "t1",
        "node_key": "错误码E502",
        "new_standard_name": "错误码E502v2",
    }
    assert "MATCH" in session.last_query
    assert "SET t.standard_name = $new_standard_name" in session.last_query
    # 必须是 MATCH+SET 原地改属性，不能是先删再建——删了再建会让节点
    # 已有的关系边找不到挂载对象，变成孤儿边
    assert "DELETE" not in session.last_query
    assert "CREATE" not in session.last_query


async def test_delete_term_node_sends_detach_delete_query():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_term_node(tenant_id="t1", node_key="废弃术语")

    assert session.last_parameters == {"tenant_id": "t1", "node_key": "废弃术语"}
    assert "DETACH DELETE" in session.last_query


async def test_migrate_relation_type_edges_sends_expected_query():
    session = FakeSession(rows=[{"migrated_count": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.migrate_relation_type_edges(
        tenant_id="t1", old_type="PRECEDES", new_type="COMES_BEFORE"
    )

    assert count == 3
    assert session.last_parameters == {"tenant_id": "t1"}
    assert "MATCH (a)-[r:PRECEDES {tenant_id: $tenant_id}]->(b)" in session.last_query
    assert "CREATE (a)-[r2:COMES_BEFORE]->(b)" in session.last_query


async def test_migrate_relation_type_edges_rejects_invalid_old_type():
    client = Neo4jGraphClient(driver=FakeDriver(FakeSession(rows=[])))

    with pytest.raises(ValueError):
        await client.migrate_relation_type_edges(
            tenant_id="t1", old_type="bad-name", new_type="GOOD_NAME"
        )


async def test_migrate_relation_type_edges_rejects_invalid_new_type():
    client = Neo4jGraphClient(driver=FakeDriver(FakeSession(rows=[])))

    with pytest.raises(ValueError):
        await client.migrate_relation_type_edges(
            tenant_id="t1", old_type="PRECEDES", new_type="bad-name"
        )


async def test_migrate_relation_type_edges_rejects_injection_attack_payloads():
    client = Neo4jGraphClient(driver=FakeDriver(FakeSession(rows=[])))

    # Test that injection-shaped payloads are rejected, not just format violations
    injection_payloads = [
        "PRECEDES]-[HACKED",  # Cypher bracket injection attempt
        "PRECEDES;DROP",      # Semicolon injection attempt
        "PRECEDES`",          # Backtick injection attempt
    ]

    for payload in injection_payloads:
        with pytest.raises(ValueError):
            await client.migrate_relation_type_edges(
                tenant_id="t1", old_type=payload, new_type="SAFE_TYPE"
            )


async def test_migrate_relation_type_edges_rejects_trailing_newline():
    """回归测试：Python 的 $ 在没有 re.MULTILINE 的情况下，仍然会匹配字符串末尾
    紧邻的一个换行符之前的位置，'PRECEDES\\n' 这种 payload 会被 .match() 放过——
    改用 \\Z 后必须拒绝。"""
    client = Neo4jGraphClient(driver=FakeDriver(FakeSession(rows=[])))

    with pytest.raises(ValueError):
        await client.migrate_relation_type_edges(
            tenant_id="t1", old_type="PRECEDES\n", new_type="SAFE_TYPE"
        )


async def test_migrate_term_type_nodes_sends_expected_query():
    session = FakeSession(rows=[{"migrated_count": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.migrate_term_type_nodes(
        tenant_id="t1", old_type="旧类型", new_type="新类型"
    )

    assert count == 3
    assert session.last_parameters == {
        "tenant_id": "t1", "old_type": "旧类型", "new_type": "新类型",
    }
    assert "MATCH (t:Term {tenant_id: $tenant_id, type: $old_type})" in session.last_query
    assert "SET t.type = $new_type" in session.last_query


async def test_migrate_term_type_nodes_returns_zero_when_no_matching_nodes():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.migrate_term_type_nodes(
        tenant_id="t1", old_type="旧类型", new_type="新类型"
    )

    assert count == 0


async def test_count_non_iso_date_values_counts_entities_not_distinct_values():
    """多个实体共享同一个非法值时，count 必须是实体数，不是不同取值数。

    这条钉住 Task 7 评审 C1：409 文案说的是"还有 N 个实体"，如果 Cypher 用
    DISTINCT 去重计数，100 个实体共享同一个占位值"待定"时会报成"1 个"，
    管理员会把大规模数据问题误判成孤立小问题。这里两个实体都是"待定"、
    一个是"2026/1/15"，去重后只有 2 个不同取值，但不合格的实体一共 3 个。
    """
    session = FakeSession(
        rows=[{"value": "待定"}, {"value": "待定"}, {"value": "2026/1/15"}]
    )
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count, samples = await client.count_non_iso_date_values(
        tenant_id="t1", term_type="订单", field="purchase_date"
    )

    assert count == 3
    # 样例仍然去重展示，避免同一个占位值反复占满 3 个名额。
    assert samples == ["待定", "2026/1/15"]
    assert "DISTINCT" not in session.last_query
    assert "t.purchase_date" in session.last_query
    assert session.last_parameters == {"tenant_id": "t1", "term_type": "订单"}


async def test_count_non_iso_date_values_treats_non_string_values_as_invalid():
    """历史脏数据里字段值不是字符串时（正常写入路径不会产生），一律算不合格。

    is_normalized_date 要求 str 输入，直接传非字符串值会报错，不能假装它
    合格放过去——放过去等于把这类脏数据的检测责任推给了别处。
    """
    session = FakeSession(rows=[{"value": 20260115}, {"value": "2026-01-15"}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count, samples = await client.count_non_iso_date_values(
        tenant_id="t1", term_type="订单", field="purchase_date"
    )

    assert count == 1
    assert samples == ["20260115"]


async def test_count_non_iso_date_values_returns_zero_for_clean_values():
    session = FakeSession(rows=[{"value": "2026-01-05"}, {"value": "2026-10-05"}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count, samples = await client.count_non_iso_date_values(
        tenant_id="t1", term_type="订单", field="purchase_date"
    )

    assert (count, samples) == (0, [])


async def test_count_non_iso_date_values_rejects_invalid_field_name():
    """field 插值进 Cypher 前必须先过格式校验，不能无条件拼接。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    with pytest.raises(ValueError):
        await client.count_non_iso_date_values(
            tenant_id="t1", term_type="订单", field="purchase_date`} MATCH (n) DETACH DELETE n //"
        )
    assert session.calls == [], "格式校验必须在发起查询之前，不合法的字段名不能触发任何往返"


async def test_sync_term_merges_by_tenant_and_node_key():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        tenant_id="t1", node_key="k1", standard_name="错误码E502",
        aliases=["网关超时"], term_type="error_code",
    )

    await client.sync_term(term)

    assert session.last_parameters["tenant_id"] == "t1"
    assert session.last_parameters["node_key"] == "k1"
    assert session.last_parameters["standard_name"] == "错误码E502"
    assert "MERGE (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query
    assert "SET t.standard_name = $standard_name" in session.last_query


async def test_query_subgraph_matches_by_tenant_and_node_key():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("k1", tenant_id="t1", chain_query_relation_types=_CHAIN_TYPES)

    assert "MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query
    assert session.last_parameters["node_key"] == "k1"
    assert session.last_parameters["tenant_id"] == "t1"


async def test_merge_relation_scopes_node_merge_by_tenant():
    """merge_relation 的两端节点 MERGE 现在也要带 tenant_id——不这样做的话
    两个租户各自抽取出同名术语时会共用同一个 Neo4j 节点，是本次改造要
    解决的核心问题（docs/EXECUTION_PLAN.md 第9节列为"尚未做的"欠账）。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_node_key="错误码E502", object_node_key="登录模块",
        relation_type="RELATED_TO", source="a.md", tenant_id="t1",
        provenance="auto_merged", recorded_at=_NOW,
    )

    assert "MERGE (a:Term {tenant_id: $tenant_id, node_key: $subject_name})" in session.last_query
    assert "MERGE (b:Term {tenant_id: $tenant_id, node_key: $object_name})" in session.last_query


async def test_rename_term_node_updates_standard_name_not_node_key():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.rename_term_node(
        tenant_id="t1", node_key="k1", new_standard_name="错误码E502v2"
    )

    assert session.last_parameters == {
        "tenant_id": "t1", "node_key": "k1", "new_standard_name": "错误码E502v2",
    }
    assert "MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query
    assert "SET t.standard_name = $new_standard_name" in session.last_query


async def test_delete_term_node_scopes_by_tenant():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_term_node(tenant_id="t1", node_key="k1")

    assert session.last_parameters == {"tenant_id": "t1", "node_key": "k1"}
    assert "MATCH (t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query


async def test_ensure_extra_field_indexes_creates_index_per_scalar_field():
    from app.graphrag.ontology_categories import ExtraFieldSpec

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_extra_field_indexes(
        tenant_id="muji", term_type="Product",
        extra_fields=[
            ExtraFieldSpec(name="numeric_value", value_type="number"),
            ExtraFieldSpec(name="dims", value_type="number[]"),
            ExtraFieldSpec(name="md_no", value_type="string"),
        ],
    )

    queries = [call[0] for call in session.calls]
    assert any("t.numeric_value" in q for q in queries)
    assert any("t.md_no" in q for q in queries)
    assert not any("t.dims" in q for q in queries)  # number[] 不建标量索引，见 spec 第6节
    assert len(queries) == 2
    # 索引匿名创建（不显式命名）——term_type 未经字符集校验，不能拼进索引名字符串，
    # 见 ensure_extra_field_indexes 的说明。断言查询文本里只有固定的
    # "CREATE INDEX IF NOT EXISTS" 前缀，不含由 term_type 拼出的自定义索引名。
    assert all(q.startswith("CREATE INDEX IF NOT EXISTS FOR (t:Term) ON") for q in queries)
    assert not any("Product" in q for q in queries)


async def test_ensure_extra_field_indexes_tolerates_unsanitized_term_type():
    """term_type（分类枚举值）不经过任何字符集校验，可能含空格/标点——索引匿名创建，
    term_type 不会被拼进 Cypher 语句文本，所以即使传一个"脏"值也不会产生格式非法的
    CREATE INDEX 语句。"""
    from app.graphrag.ontology_categories import ExtraFieldSpec

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_extra_field_indexes(
        tenant_id="muji", term_type="Weird Type; DROP",
        extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
    )

    queries = [call[0] for call in session.calls]
    assert queries == [
        "CREATE INDEX IF NOT EXISTS FOR (t:Term) ON (t.tenant_id, t.type, t.numeric_value)"
    ]


async def test_date_fields_get_an_index():
    """漏了 date 的话功能照样对，只是每次范围过滤全表扫——而范围过滤
    恰恰是最需要索引的那类查询，压测之前谁也看不出来。"""
    from app.graphrag.ontology_categories import ExtraFieldSpec

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_extra_field_indexes(
        tenant_id="demo", term_type="订单",
        extra_fields=[ExtraFieldSpec(name="purchase_date", value_type="date", label="")],
    )

    queries = [call[0] for call in session.calls]
    assert any("purchase_date" in q for q in queries)


async def test_ensure_tenant_scoped_schema_creates_indexes_and_backfills_legacy_nodes():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_tenant_scoped_schema()

    queries = [call[0] for call in session.calls]
    assert any("CREATE INDEX" in q and "IF NOT EXISTS" in q and "tenant_id" in q and "node_key" in q for q in queries)
    assert any("CREATE INDEX" in q and "IF NOT EXISTS" in q and "term_type" in q or "t.type" in q for q in queries)
    assert any(
        "WHERE t.tenant_id IS NULL" in q and "SET t.tenant_id = 'default'" in q and "t.node_key = t.standard_name" in q
        for q in queries
    )


async def test_execute_structured_filter_query_builds_attribute_where_clause():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[
        {"standard_name": "SKU A", "node_key": "SKU:1", "term_type": "SKU",
         "all_properties": {"numeric_value": 600}},
    ])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        expand=None, group_by=None, limit=20,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert result["rows"] == [
        {"standard_name": "SKU A", "node_key": "SKU:1", "term_type": "SKU",
         "all_properties": {"numeric_value": 600}},
    ]
    assert session.last_parameters["tenant_id"] == "muji"
    assert session.last_parameters["anchor_term_type"] == "SKU"
    assert "field_0" not in session.last_parameters  # 字段名现在直接插值进查询文本，不再是运行时参数
    assert session.last_parameters["value_0"] == 500
    assert session.last_parameters["limit"] == 20
    assert "anchor.numeric_value" in session.last_query
    assert "> $value_0" in session.last_query


async def test_execute_structured_filter_query_builds_relation_exists_subquery():
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
            target_field="raw_value", target_operator="eq", target_value="红",
        )],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
        },
    )

    assert "EXISTS {" in session.last_query
    assert "-[:HAS_VARIANT]->" in session.last_query
    assert "c0_hop0.raw_value = $c0_target_value" in session.last_query
    assert "c0_target_field" not in session.last_parameters
    assert session.last_parameters["c0_target_value"] == "红"


async def test_execute_structured_filter_query_incoming_direction_reverses_arrow():
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="VariantValue"),
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="incoming", target_term_type="SKU")],
            target_field="price", target_operator="gt", target_value=0,
        )],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="VariantValue", node_key=None), tenant_id="muji",
        term_type_schema={
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
        },
    )

    assert "<-[:HAS_VARIANT]-" in session.last_query


async def test_execute_structured_filter_query_group_by_returns_aggregated_groups():
    from app.graphrag.structured_filter_query import GroupBy, Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[{"value": "红色", "count": 12}, {"value": "白色", "count": 8}])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[RelationConstraint(
            hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
            target_field="raw_value", target_operator="eq", target_value="__group__",
        )],
        expand=None, group_by=GroupBy(constraint_index=0), limit=20,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
        },
    )

    assert result == {"groups": [{"value": "红色", "count": 12}, {"value": "白色", "count": 8}]}
    assert "count(DISTINCT anchor)" in session.last_query
    assert "RETURN g0_hop0.raw_value AS value" in session.last_query
    assert "group_field" not in session.last_parameters


async def test_execute_structured_filter_query_array_operator_uses_list_predicate():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[AttributeConstraint(field="dims", operator="all_lte", value=80)],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert "all(x IN anchor.dims WHERE x <= $value_0)" in session.last_query


async def test_execute_structured_filter_query_two_relation_constraints_build_independent_exists():
    """同一个锚点上挂两个互相独立的 kind=relation 约束——两段 EXISTS 子查询必须各自
    用不同的 hop 变量前缀（c0_/c1_），否则第二段会复用第一段的变量、把两个本该独立
    的分支条件错误地绑成同一条路径。"""
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[
            RelationConstraint(
                hops=[Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue")],
                target_field="raw_value", target_operator="eq", target_value="红",
            ),
            RelationConstraint(
                hops=[Hop(relation_type="BELONGS_TO", direction="outgoing", target_term_type="Category")],
                target_field="numeric_value", target_operator="gt", target_value=500,
            ),
        ],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
            "Category": TermTypeCategory(value="Category", extra_fields=[]),
        },
    )

    assert session.last_query.count("EXISTS {") == 2
    assert "MATCH (anchor)-[:HAS_VARIANT]->(c0_hop0:Term {tenant_id: $tenant_id, type: $c0_type0})" in session.last_query
    assert "MATCH (anchor)-[:BELONGS_TO]->(c1_hop0:Term {tenant_id: $tenant_id, type: $c1_type0})" in session.last_query
    assert "c0_hop0.raw_value = $c0_target_value" in session.last_query
    assert "c1_hop0.numeric_value > $c1_target_value" in session.last_query
    assert session.last_parameters["c0_type0"] == "VariantValue"
    assert session.last_parameters["c1_type0"] == "Category"
    assert session.last_parameters["c0_target_value"] == "红"
    assert session.last_parameters["c1_target_value"] == 500


async def test_execute_structured_filter_query_two_hop_chain_targets_last_hop_variable():
    """2 跳链式约束：MATCH 模式要把两跳串起来，最终的属性比较必须落在最后一跳的
    变量（c0_hop1）上，不能错落在中间跳（c0_hop0）上。"""
    from app.graphrag.structured_filter_query import Hop, RelationConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[RelationConstraint(
            hops=[
                Hop(relation_type="HAS_VARIANT", direction="outgoing", target_term_type="VariantValue"),
                Hop(relation_type="BELONGS_TO", direction="outgoing", target_term_type="Category"),
            ],
            target_field="numeric_value", target_operator="gte", target_value=500,
        )],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="muji",
        term_type_schema={
            "SKU": TermTypeCategory(value="SKU", extra_fields=[]),
            "VariantValue": TermTypeCategory(value="VariantValue", extra_fields=[]),
            "Category": TermTypeCategory(value="Category", extra_fields=[]),
        },
    )

    assert (
        "MATCH (anchor)-[:HAS_VARIANT]->(c0_hop0:Term {tenant_id: $tenant_id, type: $c0_type0})"
        "-[:BELONGS_TO]->(c0_hop1:Term {tenant_id: $tenant_id, type: $c0_type1})"
    ) in session.last_query
    assert "c0_hop1.numeric_value >= $c0_target_value" in session.last_query
    assert "c0_hop0.numeric_value" not in session.last_query
    assert session.last_parameters["c0_type0"] == "VariantValue"
    assert session.last_parameters["c0_type1"] == "Category"


async def test_execute_structured_filter_query_casts_numeric_standard_name_comparison():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="销量"),
        constraints=[AttributeConstraint(field="standard_name", operator="gt", value=50)],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="销量", node_key=None), tenant_id="demo",
        term_type_schema={"销量": TermTypeCategory(value="销量", extra_fields=[], standard_name_value_type="number")},
    )

    assert "toFloat(anchor.standard_name)" in session.last_query


async def test_execute_structured_filter_query_does_not_cast_string_standard_name_comparison():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[AttributeConstraint(field="standard_name", operator="starts_with", value="圆角")],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="demo",
        term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert "toFloat(" not in session.last_query
    assert "toInteger(" not in session.last_query


async def test_execute_structured_filter_query_does_not_cast_extra_field_comparison():
    """extra_fields 数值属性在 Neo4j 里本来就是按声明类型写入的，不需要运行时转换——
    只有 standard_name（节点自身的名字/取值，物理上恒为字符串）才需要。"""
    from app.graphrag.ontology_categories import ExtraFieldSpec
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="demo",
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )},
    )

    assert "toFloat(" not in session.last_query


async def test_execute_structured_filter_query_returns_real_total_count_beyond_limit():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    # FakeSession 现在需要按调用顺序返回不同结果——第一次调用（计数查询）返回
    # total，第二次调用（取行查询）返回受 limit 截断的行。见上面对 FakeSession 的改动
    # （call_results 是新增的可选参数，按调用顺序消费，跟现有大多数测试用的
    # rows= 参数是两种独立的构造方式，不是同一个参数改了名字）。
    session = FakeSession(call_results=[{"total": 42}, [
        {"standard_name": f"SKU {i}", "node_key": f"SKU:{i}", "term_type": "SKU", "all_properties": {}}
        for i in range(2)
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        expand=None, group_by=None, limit=2,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None), tenant_id="demo",
        term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert result["total_count"] == 42
    assert len(result["rows"]) == 2


async def test_execute_structured_filter_query_limit_zero_skips_rows_query():
    # limit=0 是"只要计数、不要样本"的信号（见 tool.py 的
    # _PARAMETERS_SCHEMA.limit 说明）——只应该发出一次 .run()（计数查询），
    # 不应该再额外发出取行查询，session.calls 只有 1 条记录了这一点。
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 10000}])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="订单号"),
        constraints=[AttributeConstraint(field="standard_name", operator="starts_with", value="0")],
        expand=None, group_by=None, limit=0,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="订单号", node_key=None), tenant_id="demo",
        term_type_schema={},
    )

    assert result == {"rows": [], "total_count": 10000}
    assert len(session.calls) == 1


async def test_execute_structured_filter_query_name_anchor_matches_by_node_key():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "Coca-Cola", "node_key": "公司:Coca-Cola", "term_type": "公司", "all_properties": {}},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="公司"),  # 这一步 anchor 字段本身不再被 execute_structured_filter_query 使用
        constraints=[], expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="公司", node_key="公司:Coca-Cola"),
        tenant_id="demo", term_type_schema={},
    )

    assert "node_key: $anchor_node_key" in session.calls[-1][0]
    assert session.calls[-1][1]["anchor_node_key"] == "公司:Coca-Cola"


async def test_execute_structured_filter_query_type_anchor_matches_by_type():
    from app.graphrag.structured_filter_query import AttributeConstraint, StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 0}, []])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="SKU"),
        constraints=[AttributeConstraint(field="numeric_value", operator="gt", value=500)],
        expand=None, group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="SKU", node_key=None),
        tenant_id="demo", term_type_schema={"SKU": TermTypeCategory(value="SKU", extra_fields=[])},
    )

    assert "type: $anchor_term_type" in session.calls[-1][0]


async def test_execute_structured_filter_query_expand_any_relation_type_omits_type_segment():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="both"),
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    query = session.calls[-1][0]
    assert "OPTIONAL MATCH" in query
    assert "[r*1..1]" in query
    # 两侧都要有基础横杠、且都不能带箭头——同时证明没有方向箭头，也证明基础横杠没丢
    # （方向映射漏掉横杠的那个 bug，正好是这个断言要防的）
    assert "-[r*1..1]-(" in query
    assert ":" not in query.split("[r")[1].split("*")[0]  # 关系类型段为空


async def test_execute_structured_filter_query_expand_specific_relation_type_includes_type_segment():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type="BELONG_TO", direction="outgoing"),
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    query = session.calls[-1][0]
    # 带基础横杠的完整箭头写法——不带横杠的 "[r:BELONG_TO*1..1]->" 也会被旧 bug
    # （方向映射漏掉基础横杠）满足，所以断言必须包含前导 "-"
    assert "-[r:BELONG_TO*1..1]->" in query


async def test_execute_structured_filter_query_expand_direction_incoming_uses_left_arrow():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="incoming"),
        group_by=None, limit=20,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    assert "<-[r*1..1]-" in session.calls[-1][0]


async def test_execute_structured_filter_query_expand_limit_applies_before_optional_match():
    """LIMIT 必须约束的是锚点数，不是展开后的行数——WITH...LIMIT 必须出现在
    OPTIONAL MATCH 之前。"""
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, []])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="both"),
        group_by=None, limit=5,
    )

    await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    query = session.calls[-1][0]
    assert query.index("LIMIT $limit") < query.index("OPTIONAL MATCH")


async def test_execute_structured_filter_query_expand_returns_empty_list_when_no_neighbors():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}, "neighbors": []},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=ExpandSpec(hops=1, relation_type=None, direction="both"),
        group_by=None, limit=20,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    assert result["rows"][0]["neighbors"] == []


async def test_execute_structured_filter_query_no_expand_rows_have_no_neighbors_key():
    from app.graphrag.structured_filter_query import StructuredFilterQueryArgs

    session = FakeSession(call_results=[{"total": 1}, [
        {"standard_name": "x", "node_key": "k", "term_type": "T", "all_properties": {}},
    ]])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    args = StructuredFilterQueryArgs(
        anchor=TypeAnchor(term_type="T"),
        constraints=[AttributeConstraint(field="standard_name", operator="eq", value="x")],
        expand=None, group_by=None, limit=20,
    )

    result = await client.execute_structured_filter_query(
        args, resolved=ResolvedAnchor(term_type="T", node_key=None),
        tenant_id="demo", term_type_schema={"T": TermTypeCategory(value="T", extra_fields=[])},
    )

    assert "neighbors" not in result["rows"][0]


async def test_probe_relation_fanout_returns_max_distinct_targets():
    session = FakeSession(rows=[{"fanout": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    fanout = await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    )

    assert fanout == 3
    assert session.last_parameters == {
        "tenant_id": "demo", "from_term_type": "产品", "to_term_type": "公司",
    }
    # relation_type 只能插值（Cypher 不支持参数化关系类型），term_type 必须参数化。
    assert "[r:BELONG_TO]" in session.last_query
    assert "$from_term_type" in session.last_query
    assert "(a:Term)-[r:BELONG_TO]->(b:Term)" in session.last_query
    # 关系边本身也要按租户过滤，跟 query_subgraph 的 WHERE r.tenant_id 一致。
    assert "r.tenant_id = $tenant_id" in session.last_query


async def test_probe_relation_fanout_flips_the_pattern_for_incoming():
    session = FakeSession(rows=[{"fanout": 1}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="公司", to_term_type="产品", direction="incoming",
    )

    assert "(a:Term)<-[r:BELONG_TO]-(b:Term)" in session.last_query


async def test_probe_relation_fanout_returns_zero_when_no_edges_match():
    # 没有任何匹配边时，WITH 阶段产出 0 行，max() 在空输入上返回 null——
    # Cypher 仍然会给出一行、fanout 为 None，不能直接返回 None。
    session = FakeSession(rows=[{"fanout": None}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    assert await client.probe_relation_fanout(
        tenant_id="demo", relation_type="BELONG_TO",
        from_term_type="产品", to_term_type="公司", direction="outgoing",
    ) == 0


async def test_delete_relation_edge_matches_one_direction_and_returns_removed_count():
    """按业务键定位一条边：起点 node_key + 关系类型 + 终点 node_key + 租户。
    Neo4j 内部 id 不稳定（重启/重建后会变），不能拿来当句柄。

    模式必须是有向的——(a)-[r]->(b) 和 (b)-[r]->(a) 是两条不同的边，无向
    匹配会让"删掉 A 指向 B 的那条"顺手把 B 指向 A 的那条也删了。"""
    session = FakeSession(rows=[{"removed": 2}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    removed = await client.delete_relation_edge(
        tenant_id="t1",
        subject_node_key="错误码E502",
        relation_type="RELATED_TO",
        object_node_key="示例登录模块",
    )

    assert removed == 2
    assert session.last_parameters == {
        "tenant_id": "t1",
        "subject_node_key": "错误码E502",
        "relation_type": "RELATED_TO",
        "object_node_key": "示例登录模块",
    }
    assert (
        "MATCH (a:Term {tenant_id: $tenant_id, node_key: $subject_node_key})"
        "-[r]->(b:Term {tenant_id: $tenant_id, node_key: $object_node_key})"
    ) in session.last_query
    assert "DELETE r" in session.last_query


async def test_delete_relation_edge_only_deletes_edges_of_this_tenant():
    """边自己的 tenant_id 也要进过滤条件——两端节点属于本租户、边却标着
    别的租户的历史脏数据是真实存在的（见 _TERM_RELATIONS_QUERY 的同款
    说明），删除路径不能顺手动别的租户的边。"""
    session = FakeSession(rows=[{"removed": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_relation_edge(
        tenant_id="t1", subject_node_key="a", relation_type="RELATED_TO", object_node_key="b",
    )

    assert "r.tenant_id = $tenant_id" in session.last_query


async def test_delete_relation_edge_passes_relation_type_as_a_parameter():
    """关系类型走参数（WHERE type(r) = $relation_type），不拼进查询文本。
    这个值来自 HTTP 请求，插值就等于把外部输入拼进 Cypher；本文件里其它
    做插值的地方（execute_structured_filter_query/probe_relation_fanout）都
    依赖调用方先跑过白名单校验，删边这条路径没有那样一份白名单。"""
    session = FakeSession(rows=[{"removed": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_relation_edge(
        tenant_id="t1",
        subject_node_key="a",
        relation_type="EVIL_TYPE",
        object_node_key="b",
    )

    assert "EVIL_TYPE" not in session.last_query
    assert "type(r) = $relation_type" in session.last_query


async def test_delete_relation_edge_returns_zero_when_no_rows():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    removed = await client.delete_relation_edge(
        tenant_id="t1", subject_node_key="a", relation_type="RELATED_TO", object_node_key="b",
    )

    assert removed == 0


async def test_ensure_tenant_scoped_schema_backfills_legacy_relation_edges():
    """节点回填只 SET 节点，边上的 tenant_id 一直没人补——旧库里可能仍有
    tenant_id 为 null 的关系边，它们被详情页（_TERM_RELATIONS_QUERY）和删除
    影响面预演（summarize_relation_edges_for_terms）一致地忽略，同时也删不掉
    （删边接口按边的 tenant_id 过滤）——只存在于库里、界面上无从查证也无从
    处置。

    只回填"两端节点同租户、边自己没有 tenant_id"这一类（A 类）：这类边的
    归属没有歧义，补的正是 merge_relation 写入时本就该有的那个值。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_tenant_scoped_schema()

    backfills = [q for q, _ in session.calls if "SET r.tenant_id" in q]
    assert len(backfills) == 1
    query = backfills[0]
    assert "r.tenant_id IS NULL" in query
    assert "a.tenant_id = b.tenant_id" in query
    assert "SET r.tenant_id = a.tenant_id" in query


async def test_relation_edge_backfill_leaves_mismatched_and_cross_tenant_edges_alone():
    """B 类（边的租户和两端节点对不上）和 C 类（两端节点分属不同租户）
    绝不能被自动"修正"——那等于让一批今天被一致忽略的边突然活过来参与
    检索和守卫，悄悄改变租户隔离边界。

    这两条断言得能真的区分：去掉 IS NULL 守卫，B 类边会被覆盖成节点的
    租户；去掉两端同租户的条件，C 类边会被随便挑一端的租户染上。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_tenant_scoped_schema()

    query = next(q for q, _ in session.calls if "SET r.tenant_id" in q)
    conditions = query.split("SET")[0]
    assert "r.tenant_id IS NULL" in conditions
    assert "a.tenant_id = b.tenant_id" in conditions
    # ALIAS_OF 是术语表→图谱的结构性同步边，不参与租户语义（sync_term 写
    # 别名边时根本不设 tenant_id），给它补一个租户属性等于凭空发明语义。
    assert "type(r) <> 'ALIAS_OF'" in conditions


async def test_ensure_tenant_scoped_schema_is_idempotent_across_two_runs():
    """跑两次和跑一次发出的语句完全相同：所有语句都是幂等的
    （CREATE INDEX IF NOT EXISTS / 带 IS NULL 守卫的两条回填），没有
    任何一条会因为上一次跑过而变成另一个样子或多做一次写入。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_tenant_scoped_schema()
    first_run = list(session.calls)
    session.calls.clear()
    await client.ensure_tenant_scoped_schema()

    assert list(session.calls) == first_run
    # 第二次跑之所以是 no-op，靠的是两条回填语句各自的 IS NULL 守卫——
    # 第一次跑完之后就没有节点/边还满足它们的匹配条件了。
    writes = [q for q, _ in first_run if "SET " in q]
    assert len(writes) == 2
    assert all("IS NULL" in q for q in writes)


class SurveySession(FakeSession):
    """普查那条语句返回指定的行，其余语句照 FakeSession 的老样子。

    按语句内容分发而不是按调用序号：ensure_tenant_scoped_schema 里语句的
    条数和顺序以后还会变，序号绑定的测试会在与本意无关的改动上碎掉。"""

    def __init__(self, survey_rows: list[dict]) -> None:
        super().__init__(rows=[])
        self._survey_rows = survey_rows

    async def run(self, query: str, parameters: dict | None = None) -> FakeResult:
        result = await super().run(query, parameters)
        if "AS samples" in query:
            return FakeResult(self._survey_rows)
        return result


async def test_ensure_tenant_scoped_schema_warns_about_inconsistent_relation_edges(caplog):
    """B 类（边的租户和两端节点对不上）和 C 类（两端节点跨租户）不自动改，
    那就必须让人看得见——否则它们永远停在"既不参与检索、也删不掉、还没人
    知道它存在"的状态里，正是本项目的头号反模式。

    日志得说清楚：各有多少条、样本是哪几条（两端的 node_key 与租户、边自己
    的租户）、以及它们现在的处境和该去哪儿处理。"""
    session = SurveySession(
        [
            {
                "category": "edge_tenant_mismatch",
                "total": 5,
                "samples": [
                    {
                        "subject_tenant_id": "default", "subject_node_key": "t:错误码E502",
                        "relation_type": "RELATED_TO",
                        "object_tenant_id": "default", "object_node_key": "t:登录模块",
                        "edge_tenant_id": "demo",
                    }
                ],
            },
            {
                "category": "cross_tenant",
                "total": 2,
                "samples": [
                    {
                        "subject_tenant_id": "muji", "subject_node_key": "t:A",
                        "relation_type": "PART_OF",
                        "object_tenant_id": "demo", "object_node_key": "t:B",
                        "edge_tenant_id": "muji",
                    }
                ],
            },
        ]
    )
    client = Neo4jGraphClient(driver=FakeDriver(session))

    with caplog.at_level(logging.WARNING, logger="app.graphrag.neo4j_client"):
        await client.ensure_tenant_scoped_schema()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    joined = "\n".join(warnings)
    assert "5 条" in joined
    assert "2 条" in joined
    # 样本必须点名具体是哪条边，光报数字的话运维还是找不到它
    assert "t:错误码E502" in joined and "t:登录模块" in joined
    assert "RELATED_TO" in joined and "demo" in joined
    assert "t:A" in joined and "t:B" in joined and "muji" in joined
    # 处境 + 出路：它们今天既不参与检索也不参与删除守卫，得有人工处理的去处
    assert "检索" in joined
    assert "实体详情页" in joined


async def test_ensure_tenant_scoped_schema_stays_quiet_when_no_inconsistent_edges(caplog):
    """零条时不许打日志：每次启动刷一条"一切正常"，真出问题那天这条警告
    就淹没在噪音里没人看了。"""
    session = SurveySession([])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    with caplog.at_level(logging.WARNING, logger="app.graphrag.neo4j_client"):
        await client.ensure_tenant_scoped_schema()

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_inconsistent_relation_edge_survey_query_covers_both_dirty_classes():
    """普查语句本身：B 类（边租户 != 节点租户）和 C 类（两端节点跨租户）
    都要被数到，且健康的边一条都不能被算进去。"""
    session = SurveySession([])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_tenant_scoped_schema()

    query = next(q for q, _ in session.calls if "AS samples" in q)
    # 断言必须落在 WHERE 那一段上：跨租户的条件在下面的 CASE 里也出现一次，
    # 对整条语句做 in 判断时，把 WHERE 里的它删掉测试照样是绿的。
    where_clause = query.split("WITH CASE")[0]
    assert "a.tenant_id <> b.tenant_id" in where_clause
    assert "r.tenant_id <> a.tenant_id" in where_clause
    assert "type(r) <> 'ALIAS_OF'" in where_clause
    # 分类必须由查询本身给出，否则调用方没法分别报两类的条数
    assert "'cross_tenant'" in query
    assert "'edge_tenant_mismatch'" in query


async def test_list_inconsistent_relation_edges_finds_edges_the_detail_page_hides():
    """详情页和守卫都按边的 tenant_id 过滤，脏边在界面上根本不出现——
    用户找不到它，也就无从删起。这个方法是它们唯一的入口：按两端节点
    的租户定位，不按边自己标的租户。"""
    session = FakeSession(rows=[
        {"direction": "out", "relation_type": "RELATED_TO", "node_key": "t:B",
         "standard_name": "B", "term_type": "t", "other_tenant_id": "default",
         "edge_tenant_id": "demo"},
    ])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    rows = await client.list_inconsistent_relation_edges(tenant_id="default", node_key="t:A")

    assert rows == [
        {"direction": "out", "relation_type": "RELATED_TO", "node_key": "t:B",
         "standard_name": "B", "term_type": "t", "other_tenant_id": "default",
         "edge_tenant_id": "demo"},
    ]
    assert session.last_parameters == {"tenant_id": "default", "node_key": "t:A"}
    # 起点节点仍然按本租户锁定——列的是"我这个实体身上挂着的脏边"，
    # 不是全库的脏边。
    assert "(t:Term {tenant_id: $tenant_id, node_key: $node_key})" in session.last_query
    # 两端各自的租户和边自己的租户都要返回：不返回的话，前端既显示不出
    # 这条边到底哪儿不对，删除时也拼不出对端节点的租户。
    assert "related.tenant_id AS other_tenant_id" in session.last_query
    assert "r.tenant_id AS edge_tenant_id" in session.last_query


async def test_list_inconsistent_relation_edges_excludes_healthy_edges():
    """健康的边（边的租户 = 两端节点的租户）不能出现在这份清单里——
    它们在详情页正常那一栏里已经能看能删，出现在这里只会让用户以为
    自己的图谱一团糟。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.list_inconsistent_relation_edges(tenant_id="default", node_key="t:A")

    where_clause = session.last_query.split("RETURN")[0]
    assert "r.tenant_id IS NULL" in where_clause
    assert "r.tenant_id <> t.tenant_id" in where_clause
    assert "related.tenant_id <> t.tenant_id" in where_clause
    assert "type(r) <> 'ALIAS_OF'" in where_clause


async def test_delete_inconsistent_relation_edge_locates_both_ends_by_their_own_tenants():
    """脏边删不掉的根因是 _DELETE_RELATION_EDGE_QUERY 按边的 tenant_id 过滤，
    而这些边的 tenant_id 恰恰是错的。这条路径改用两端节点各自的租户定位——
    节点的租户是更可靠的判据（边的租户已经被证明会说谎）。"""
    session = FakeSession(rows=[{"removed": 1}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    removed = await client.delete_inconsistent_relation_edge(
        subject_tenant_id="default", subject_node_key="t:A",
        relation_type="RELATED_TO",
        object_tenant_id="demo", object_node_key="t:B",
    )

    assert removed == 1
    assert session.last_parameters == {
        "subject_tenant_id": "default", "subject_node_key": "t:A",
        "relation_type": "RELATED_TO",
        "object_tenant_id": "demo", "object_node_key": "t:B",
    }
    assert (
        "MATCH (a:Term {tenant_id: $subject_tenant_id, node_key: $subject_node_key})"
        "-[r]->(b:Term {tenant_id: $object_tenant_id, node_key: $object_node_key})"
    ) in session.last_query
    assert "DELETE r" in session.last_query


async def test_delete_inconsistent_relation_edge_refuses_to_touch_healthy_edges():
    """这条路径不按边的 tenant_id 定位，如果不另加限制，它就成了一个能
    删任意边的后门（两端节点都指定得出来的话）。所以匹配条件里必须写死
    "只删违反不变式的边"：边没有租户、两端节点跨租户、或边的租户和起点
    对不上。健康的边一条都不能从这里删掉。"""
    session = FakeSession(rows=[{"removed": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_inconsistent_relation_edge(
        subject_tenant_id="t1", subject_node_key="a",
        relation_type="RELATED_TO", object_tenant_id="t1", object_node_key="b",
    )

    where_clause = session.last_query.split("DELETE")[0]
    assert "r.tenant_id IS NULL" in where_clause
    assert "a.tenant_id <> b.tenant_id" in where_clause
    assert "r.tenant_id <> a.tenant_id" in where_clause
    # 关系类型仍然走参数，理由同 delete_relation_edge：值来自 HTTP 请求。
    assert "type(r) = $relation_type" in where_clause


async def test_delete_inconsistent_relation_edge_passes_relation_type_as_a_parameter():
    session = FakeSession(rows=[{"removed": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_inconsistent_relation_edge(
        subject_tenant_id="t1", subject_node_key="a", relation_type="EVIL_TYPE",
        object_tenant_id="t1", object_node_key="b",
    )

    assert "EVIL_TYPE" not in session.last_query


async def test_delete_inconsistent_relation_edge_returns_zero_when_no_rows():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    removed = await client.delete_inconsistent_relation_edge(
        subject_tenant_id="t1", subject_node_key="a", relation_type="RELATED_TO",
        object_tenant_id="t1", object_node_key="b",
    )

    assert removed == 0


class ChainAwareFakeSession(FakeSession):
    """只按查询文本里有没有 `*2..2` 决定要不要返回一行 hops=2 的数据。

    仓库的 FakeSession 不解释 Cypher，无法真的按关系类型匹配。这个子类
    把「查询里问了 2 跳」和「结果里出现 2 跳行」绑在一起，用来断言"没有
    链式关系类型时不会拿到 2 跳结果"这件事本身，而不只是断言查询文本。
    """

    async def run(self, query: str, parameters: dict | None = None) -> FakeResult:
        await super().run(query, parameters)
        rows = [{"related_name": "登录模块", "relation_type": "RELATED_TO", "hops": 1}]
        if "*2..2" in query:
            rows.append(
                {"related_name": "会员资格", "relation_type": "DEPENDS_ON", "hops": 2}
            )
        return FakeResult(rows)


async def test_query_subgraph_skips_two_hop_branch_when_no_chain_relation_types():
    """一个链式关系类型都没有时，整段 UNION 不发出去。

    不能退化成 `[r:*2..2]`：那会匹配所有关系类型、无差别两跳发散，比现状
    （固定三种）更糟。
    """
    session = ChainAwareFakeSession()
    client = Neo4jGraphClient(driver=FakeDriver(session))

    results = await client.query_subgraph(
        "错误码E502", tenant_id="t1", chain_query_relation_types=set()
    )

    assert [row for row in results if row.get("hops") == 2] == []
    assert "*2..2" not in session.last_query
    assert "UNION" not in session.last_query
    # 1 跳那段必须原样还在——跳过的只是 2 跳。
    assert "-[r]-(related:Term {tenant_id: $tenant_id})" in session.last_query


async def test_query_subgraph_returns_two_hop_rows_when_chain_relation_types_given():
    """跟上一条配对：同一个 fake 下，传了链式关系类型就应该拿到 2 跳行。
    否则上一条的"没有 2 跳行"可能只是因为 fake 从来不产出 2 跳行。"""
    session = ChainAwareFakeSession()
    client = Neo4jGraphClient(driver=FakeDriver(session))

    results = await client.query_subgraph(
        "错误码E502", tenant_id="t1", chain_query_relation_types=_CHAIN_TYPES
    )

    assert [row["related_name"] for row in results if row.get("hops") == 2] == ["会员资格"]


async def test_query_subgraph_drops_malformed_chain_relation_types(caplog):
    """读出来再拼进 Cypher 的关系类型同样要过格式校验——关系类型没法参数化
    绑定，只能拼字符串，这是注入防线。不合格的跳过并记日志，不进 Cypher。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    with caplog.at_level(logging.WARNING, logger="app.graphrag.neo4j_client"):
        await client.query_subgraph(
            "k1",
            tenant_id="t1",
            chain_query_relation_types={"DEPENDS_ON", "bad-type", "X] OR true //"},
        )

    assert "[r:DEPENDS_ON*2..2]" in session.last_query
    assert "bad-type" not in session.last_query
    assert "OR true" not in session.last_query
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "bad-type" in warnings
    assert "X] OR true //" in warnings


async def test_query_subgraph_skips_two_hop_branch_when_all_chain_types_malformed():
    """全都不合格时等价于空集合：不能拼出一个空的关系类型列表 `[r:*2..2]`。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph(
        "k1", tenant_id="t1", chain_query_relation_types={"bad-type"}
    )

    assert "*2..2" not in session.last_query
    assert "UNION" not in session.last_query


async def test_query_subgraph_requires_chain_relation_types_argument():
    """不给默认值：漏传立刻 TypeError，而不是悄悄回退到写死的三种关系。"""
    client = Neo4jGraphClient(driver=FakeDriver(FakeSession(rows=[])))

    with pytest.raises(TypeError):
        await client.query_subgraph("k1", tenant_id="t1")


async def test_summarize_relation_edges_returns_the_breakdown_by_counterpart_type():
    """删除前的影响面：会连带删掉多少条边、这些边连向哪几类实体。

    一次问完整批：两万条实体逐条问就是两万次 Neo4j 往返，而这一步挡在
    确认框前面——它慢，用户就点不下去删除。
    """
    session = FakeSession(
        rows=[
            {"counterpart_type": "订单号", "edge_count": 1009},
            {"counterpart_type": "类目", "edge_count": 4},
        ]
    )
    client = Neo4jGraphClient(driver=FakeDriver(session))

    rows = await client.summarize_relation_edges_for_terms(
        tenant_id="t1", node_keys=["t:甲", "t:乙"]
    )

    assert rows == [
        {"counterpart_type": "订单号", "edge_count": 1009},
        {"counterpart_type": "类目", "edge_count": 4},
    ]
    assert len(session.calls) == 1
    assert session.last_parameters == {
        "tenant_id": "t1",
        "node_keys": ["t:甲", "t:乙"],
        "unknown_label": UNKNOWN_COUNTERPART_TYPE,
    }


async def test_summarize_relation_edges_counts_each_edge_once():
    """按边去重是这条查询的命根子：自环在无向模式下匹配两次，一条边的
    两端都在待删列表里时也会匹配两次。不去重的话分项之和就不等于“会被
    删掉的边总数”，而用户会拿它们互相验算。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.summarize_relation_edges_for_terms(tenant_id="t1", node_keys=["t:甲"])

    assert "WITH r, head(collect(other))" in session.last_query


async def test_summarize_relation_edges_uses_the_same_filter_as_the_detail_page():
    """口径必须和详情页列出的边一致：r.tenant_id 过滤 + 排除 ALIAS_OF。

    预演报的数和用户在详情页数得出来的数对不上的话，他会认为其中一个在
    说谎，而他没有办法判断是哪个。别名边算进去的话，每个有别名的实体都会
    凭空多报几条。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.summarize_relation_edges_for_terms(tenant_id="t1", node_keys=["t:甲"])

    assert "r.tenant_id = $tenant_id" in session.last_query
    assert "type(r) <> 'ALIAS_OF'" in session.last_query


async def test_summarize_relation_edges_with_empty_batch_does_not_touch_the_graph():
    """一条都没选中时不该发查询——确认框没有任何东西可说。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    rows = await client.summarize_relation_edges_for_terms(tenant_id="t1", node_keys=[])

    assert rows == []
    assert session.calls == []


async def test_delete_term_nodes_deletes_the_whole_batch_in_one_round_trip():
    """批量删图谱节点是一次 UNWIND，不是 N 次单条删除——N 次往返正是这个
    功能要消灭的东西。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_term_nodes(tenant_id="t1", node_keys=["t:甲", "t:乙", "t:丙"])

    assert len(session.calls) == 1
    assert session.last_parameters == {"tenant_id": "t1", "node_keys": ["t:甲", "t:乙", "t:丙"]}
    assert "UNWIND" in session.last_query


async def test_delete_term_nodes_with_empty_batch_does_not_touch_the_graph():
    """一条都没有时不该发查询：批量删除里"一条都没删成"是常见结果，
    那次请求不该在图谱上留下任何一次无意义的往返。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_term_nodes(tenant_id="t1", node_keys=[])

    assert session.calls == []


async def test_count_relation_edges_for_tenant_starts_from_the_term_nodes():
    """从节点侧发起，不是以关系起手扫全库。

    不是因为"走索引"：库里只有 (tenant_id, node_key) 和 (tenant_id, type)
    两条复合索引，而 Neo4j 要求查询覆盖索引的全部属性才用得上，只给
    tenant_id 一条都走不了。理由是扫描量级——节点侧扫的是这一个租户的
    Term 再展开各自的出边，关系侧扫的是全库所有租户的所有边。

    这条用例只能验查询形状（grep 一个字符串），验不了执行计划：那需要一个
    真实的 Neo4j 和 EXPLAIN。形状变了至少会红，量级论证仍然靠人。
    """
    session = FakeSession(rows=[{"edge_count": 1204883}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_tenant(tenant_id="demo")

    assert count == 1204883
    assert session.last_parameters == {"tenant_id": "demo"}
    assert "MATCH (t:Term {tenant_id: $tenant_id})" in session.last_query


async def test_count_relation_edges_for_tenant_counts_outgoing_only():
    """只数出边。无向匹配会让每条边被两端各数一次，看板上的关系数直接
    翻倍——而用户拿它跟实体详情页里数出来的边核对时会发现对不上。"""
    session = FakeSession(rows=[{"edge_count": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.count_relation_edges_for_tenant(tenant_id="demo")

    assert "-[r]->()" in session.last_query
    # 去掉出边那一段之后不该还剩一个无向匹配。直接断言 "-[r]-()" 不在
    # 原文里是不行的：出边写法 "-[r]->()" 本身不含这个子串，但把实现改成
    # 无向之后原文里就只剩 "-[r]-()"，上面那条断言已经会红——这一条钉的是
    # "既有出边又有无向"这种改法。
    assert "-[r]-()" not in session.last_query.replace("-[r]->()", "")


async def test_count_relation_edges_for_tenant_filters_edge_tenant_and_skips_alias():
    """口径跟详情页一致：r.tenant_id 过滤 + 排除 ALIAS_OF。

    别名边是词表→图谱的结构性同步边，不是知识图谱数据。算进去的话，看板上
    的关系数会比用户在任何别的地方看到的都大一截。
    """
    session = FakeSession(rows=[{"edge_count": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.count_relation_edges_for_tenant(tenant_id="demo")

    assert "r.tenant_id = $tenant_id" in session.last_query
    assert "type(r) <> 'ALIAS_OF'" in session.last_query


async def test_count_relation_edges_for_tenant_returns_zero_when_no_rows():
    """空图时 Cypher 仍会给出一行、count 为 0；但防御性地处理无行的情况
    ——返回 None 会让看板显示「null 条关系」。"""
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    assert await client.count_relation_edges_for_tenant(tenant_id="demo") == 0


async def test_list_tenant_dirty_edges_starts_from_this_tenants_terms():
    """从这个租户的 Term 起手，不是全库扫关系。

    理由跟 count_relation_edges_for_tenant 一样，也不是"走索引"：库里只有
    (tenant_id, node_key) 和 (tenant_id, type) 两条复合索引，Neo4j 要求查询
    覆盖索引的全部属性才用得上，只给 tenant_id 一条都走不了。是扫描量级——
    节点侧扫这一个租户的 Term，关系侧扫全库所有租户的所有边。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.list_tenant_dirty_edges(tenant_id="demo", limit=500)

    assert "MATCH (t:Term {tenant_id: $tenant_id})" in session.last_query
    assert session.last_parameters == {"tenant_id": "demo", "limit": 501}


async def test_list_tenant_dirty_edges_keeps_the_same_criteria_as_the_per_term_query():
    """判定脏边的三个条件跟详情页那条逐字一致。

    口径不一致的话，全局页列出来的和详情页列出来的对不上——运维在全局页
    删完，点进那个实体一看还有；或者反过来，全局页说干净的实体点进去有一屏
    红字。这里逐条断言，而不是"大致相同"。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.list_tenant_dirty_edges(tenant_id="demo", limit=500)

    query = session.last_query
    assert "r.tenant_id IS NULL" in query
    assert "related.tenant_id IS NULL" in query
    assert "r.tenant_id <> t.tenant_id" in query
    assert "related.tenant_id <> t.tenant_id" in query
    # 别名边是词表→图谱的结构性同步边，不是知识图谱数据，跟详情页一样排除。
    assert "type(r) <> 'ALIAS_OF'" in query


async def test_list_tenant_dirty_edges_asks_for_one_more_than_the_limit():
    """多要一条，用来判断有没有被截断。

    正好要 limit 条的话，"刚好 500 条"和"超过 500 条"拿到的结果一模一样
    ——而这两种情况要对运维说的话完全不同。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.list_tenant_dirty_edges(tenant_id="demo", limit=3)

    assert session.last_parameters["limit"] == 4


async def test_list_tenant_dirty_edges_reports_truncation():
    """超过上限时说出来，并且只返回 limit 条。

    默默少列的话，运维会以为脏边只有 500 条——他清完那 500 条，以为干净了。
    """
    rows = [
        {
            "subject_node_key": f"产品:P{i}", "subject_standard_name": f"P{i}",
            "relation_type": "RELATED_TO", "object_node_key": "模块:M",
            "object_standard_name": "M", "edge_tenant_id": None,
            "subject_tenant_id": "demo", "object_tenant_id": "demo",
        }
        for i in range(4)
    ]
    session = FakeSession(rows=rows)
    client = Neo4jGraphClient(driver=FakeDriver(session))

    edges, truncated = await client.list_tenant_dirty_edges(tenant_id="demo", limit=3)

    assert len(edges) == 3
    assert truncated is True


async def test_list_tenant_dirty_edges_does_not_claim_truncation_when_it_fits():
    """正好等于上限时不算截断。

    多要的那一条没回来，就说明后面没有了。报成截断的话运维会去找一批
    根本不存在的脏边。
    """
    rows = [
        {
            "subject_node_key": f"产品:P{i}", "subject_standard_name": f"P{i}",
            "relation_type": "RELATED_TO", "object_node_key": "模块:M",
            "object_standard_name": "M", "edge_tenant_id": None,
            "subject_tenant_id": "demo", "object_tenant_id": "demo",
        }
        for i in range(3)
    ]
    session = FakeSession(rows=rows)
    client = Neo4jGraphClient(driver=FakeDriver(session))

    edges, truncated = await client.list_tenant_dirty_edges(tenant_id="demo", limit=3)

    assert len(edges) == 3
    assert truncated is False


async def test_list_tenant_dirty_edges_looks_at_both_directions():
    """无向匹配，而不是只走出边。

    只走出边的理由曾经写成"两端都在扫描范围内，无向就是重复"——那个前提
    对脏边恰恰不成立：判据本身就是「对端跨租户 / 对端没有租户标记」，那种
    边的对端根本不是本租户的 Term，不在 `(t:Term {tenant_id: $tenant_id})`
    的扫描范围里。于是"本租户节点作宾语、主语属于别人"的脏边永远列不出来
    ——而实体详情页那条无向查询看得见它。同一条边一个页面有、一个页面没有。

    两端都在本租户时会各命中一次，靠 `WITH DISTINCT r` 去重，并用
    startNode/endNode 还原真实方向（而不是按遍历方向报）。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.list_tenant_dirty_edges(tenant_id="demo", limit=500)

    query = session.last_query
    assert "-[r]-(related:Term)" in query
    assert "-[r]->(related:Term)" not in query
    # 去重：不去的话，两端都在本租户的那条脏边会列两遍，运维以为有两条。
    assert "DISTINCT r" in query
    # 方向按边自己的 startNode/endNode 报，不按遍历方向——否则同一条边从
    # 哪一端遍历到，主宾就反过来。
    assert "startNode(r)" in query and "endNode(r)" in query


async def test_query_neighborhood_returns_both_endpoints_of_every_edge():
    """每一行是一条边，两端都带 node_key / 标准名 / 类型。

    这是它跟 query_subgraph 的根本区别：那个只返回终点名和最后一跳的关系
    类型（给 agent 拼文本够用），拿来画图会画出一堆从中心射出去的假边——
    一张看起来正常、拓扑却是错的图。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_neighborhood(
        "产品:Beer", tenant_id="demo", chain_query_relation_types=set()
    )

    query = session.last_query
    for column in (
        "source_node_key", "source_name", "source_type",
        "relation_type",
        "target_node_key", "target_name", "target_type",
    ):
        assert f"AS {column}" in query, column
    assert session.last_parameters == {"node_key": "产品:Beer", "tenant_id": "demo"}


async def test_query_neighborhood_unwinds_the_two_hop_path_so_middle_nodes_appear():
    """两跳要 UNWIND 出路径上的每一条边。

    只返回终点的话中间节点整个丢失——图上会出现从中心直连到两跳外的边，
    而那条边在图里根本不存在。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_neighborhood(
        "产品:Beer", tenant_id="demo", chain_query_relation_types={"HAS_SKU"}
    )

    assert "UNWIND r AS edge" in session.last_query
    # 同一条边会被多条路径命中，不去重的话图上是重边。
    assert "DISTINCT edge" in session.last_query


async def test_query_neighborhood_skips_the_two_hop_leg_when_no_chain_types():
    """一个合格的链式关系类型都没有时只查一跳。

    `[r:*2..2]` 会匹配所有关系类型，是比"固定几种"更糟的无差别两跳发散
    ——在预览页上表现为一张糊掉的图。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_neighborhood(
        "产品:Beer", tenant_id="demo", chain_query_relation_types=set()
    )

    assert "UNION" not in session.last_query
    assert "*2..2" not in session.last_query


async def test_query_neighborhood_and_subgraph_agree_on_which_types_walk_two_hops():
    """两条查询对"哪些关系能走两跳"必须给出同一个答案。

    各写各的消毒逻辑的话，用户在预览里看到 A 两跳能到 C、回去问却答不出来
    ——而他会拿这两处互相印证。这里放一个合法的和一个非法的，断言两边留下
    的是同一个。
    """
    types = {"HAS_SKU", "不合法的类型名"}
    session_a = FakeSession(rows=[])
    session_b = FakeSession(rows=[])

    await Neo4jGraphClient(driver=FakeDriver(session_a)).query_neighborhood(
        "产品:Beer", tenant_id="demo", chain_query_relation_types=types
    )
    await Neo4jGraphClient(driver=FakeDriver(session_b)).query_subgraph(
        "产品:Beer", tenant_id="demo", chain_query_relation_types=types
    )

    assert ("HAS_SKU" in session_a.last_query) == ("HAS_SKU" in session_b.last_query)
    assert "不合法的类型名" not in session_a.last_query
    assert "不合法的类型名" not in session_b.last_query


async def test_query_neighborhood_excludes_alias_edges():
    """别名边不画。

    它是词表→图谱的结构性同步边，不是知识图谱数据——画在图上只会让每个
    实体多出一串没有意义的卫星点。
    """
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_neighborhood(
        "产品:Beer", tenant_id="demo", chain_query_relation_types=set()
    )

    assert "type(r) <> 'ALIAS_OF'" in session.last_query
