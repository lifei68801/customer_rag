from datetime import datetime

import pytest

from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term

_NOW = datetime(2026, 8, 12, 12, 0, 0)


class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def data(self) -> list[dict]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.last_query: str | None = None
        self.last_parameters: dict | None = None
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, parameters: dict | None = None) -> FakeResult:
        self.last_query = query
        self.last_parameters = parameters
        self.calls.append((query, parameters))
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
        product_line="核心平台",
    )

    await client.sync_term(term)

    assert session.last_parameters == {
        "tenant_id": "t1",
        "node_key": "错误码E502",
        "standard_name": "错误码E502",
        "type": "error_code",
        "product_line": "核心平台",
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
        product_line="核心平台",
    )

    await client.sync_term(term)

    assert session.last_parameters["aliases"] == []


async def test_sync_term_writes_extra_properties():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        tenant_id="t1", node_key="错误码E502",
        standard_name="错误码E502", aliases=[], term_type="错误码",
        product_line="示例产品线", extra_properties={"严重等级": "高"},
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
            aliases=["网关超时"], term_type="error_code", product_line="核心平台",
        ),
        Term(
            tenant_id="t1", node_key="登录模块", standard_name="登录模块",
            aliases=["认证模块"], term_type="module", product_line="核心平台",
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


async def test_sync_term_merges_by_tenant_and_node_key():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        tenant_id="t1", node_key="k1", standard_name="错误码E502",
        aliases=["网关超时"], term_type="error_code", product_line="核心平台",
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


async def test_ensure_tenant_scoped_schema_creates_indexes_and_backfills_legacy_nodes():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.ensure_tenant_scoped_schema()

    queries = [call[0] for call in session.calls]
    assert any("CREATE INDEX IF NOT EXISTS" in q and "tenant_id" in q and "node_key" in q for q in queries)
    assert any("CREATE INDEX IF NOT EXISTS" in q and "term_type" in q or "t.type" in q for q in queries)
    assert any(
        "WHERE t.tenant_id IS NULL" in q and "SET t.tenant_id = 'default'" in q and "t.node_key = t.standard_name" in q
        for q in queries
    )
