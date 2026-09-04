import logging
from datetime import datetime

import pytest

from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.structured_filter_query import AttributeConstraint, ExpandSpec, ResolvedAnchor, TypeAnchor

_NOW = datetime(2026, 8, 12, 12, 0, 0)


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

    results = await client.query_subgraph("错误码E502", tenant_id="t1")

    assert results == [{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    assert session.last_parameters == {"node_key": "错误码E502", "tenant_id": "t1"}
    assert "WHERE r.tenant_id = $tenant_id" in session.last_query


async def test_merge_relation_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_standard_name="错误码E502",
        object_standard_name="登录模块",
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
            subject_standard_name="a",
            object_standard_name="b",
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
            subject_standard_name="a",
            object_standard_name="b",
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
        subject_standard_name="大床房",
        object_standard_name="酒店",
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
        subject_standard_name="Product:1001",
        object_standard_name="SKU:4901234567890",
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

    await client.query_subgraph("错误码E502", tenant_id="t1")

    assert "UNION" in session.last_query
    assert "REQUIRES|PRECEDES|PART_OF*2..2" in session.last_query
    assert "ALL(rel IN r WHERE rel.tenant_id = $tenant_id)" in session.last_query
    assert "AND related <> t" in session.last_query
    assert session.last_parameters == {"node_key": "错误码E502", "tenant_id": "t1"}


async def test_count_relation_edges_for_term_returns_edge_count():
    session = FakeSession(rows=[{"edge_count": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term(tenant_id="t1", node_key="错误码E502")

    assert count == 3
    assert session.last_parameters == {"tenant_id": "t1", "node_key": "错误码E502"}
    assert "type(r) <> 'ALIAS_OF'" in session.last_query


async def test_count_relation_edges_for_term_returns_zero_when_no_rows():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term(tenant_id="t1", node_key="孤立术语")

    assert count == 0


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

    await client.query_subgraph("k1", tenant_id="t1")

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
        subject_standard_name="错误码E502", object_standard_name="登录模块",
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


async def test_count_relation_edges_for_term_scopes_by_tenant():
    session = FakeSession(rows=[{"edge_count": 2}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term(tenant_id="t1", node_key="k1")

    assert count == 2
    assert session.last_parameters == {"tenant_id": "t1", "node_key": "k1"}


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


async def test_count_relation_edges_for_term_ignores_other_tenant_edges():
    """守卫查询必须同时按边的 tenant_id 过滤——只按两端节点的 tenant_id
    匹配的话，别的租户写的边会挡住本租户的术语删除（真实数据里就有一条
    两端节点 tenant_id=default、边自己 tenant_id=demo 的历史脏边）。

    merge_relation 写边时两端节点和边用的是同一个 $tenant_id
    （neo4j_client.py::merge_relation 的 MERGE 语句），所以「边的租户」按
    设计恒等于两端节点的租户，用 r.tenant_id 过滤不会误伤合法数据。
    """
    session = FakeSession(rows=[{"edge_count": 0}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.count_relation_edges_for_term(tenant_id="t1", node_key="k1")

    assert "r.tenant_id = $tenant_id" in session.last_query


async def test_count_relation_edges_for_term_counts_each_edge_once():
    """同一条边不能被数两次。无向模式 (t)-[r]-() 在自环上会产出两行、
    绑定的却是同一条边，count(r) 会把 1 条边报成 2 条；count(DISTINCT r)
    按边去重。"""
    session = FakeSession(rows=[{"edge_count": 1}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.count_relation_edges_for_term(tenant_id="t1", node_key="k1")

    assert "count(DISTINCT r) AS edge_count" in session.last_query


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
    别的租户的历史脏数据是真实存在的（见 count_relation_edges_for_term 的
    同款说明），删除路径不能顺手动别的租户的边。"""
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
    tenant_id 为 null 的关系边，它们被守卫（count_relation_edges_for_term）
    和详情页（_TERM_RELATIONS_QUERY）一致地忽略，同时也删不掉（删边接口
    按边的 tenant_id 过滤），用户的实体删除会卡死在一条他看不见的边上。

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
