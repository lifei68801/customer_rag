from datetime import datetime

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
    assert session.last_parameters == {"standard_name": "错误码E502", "tenant_id": "t1"}
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
        standard_name="错误码E502",
        aliases=["网关超时", "E502超时"],
        term_type="error_code",
        product_line="核心平台",
    )

    await client.sync_term(term)

    assert session.last_parameters == {
        "standard_name": "错误码E502",
        "type": "error_code",
        "product_line": "核心平台",
        "aliases": ["网关超时", "E502超时"],
    }
    assert "ALIAS_OF" in session.last_query
    assert "alias_name" in session.last_query


async def test_sync_term_with_no_aliases_sends_empty_alias_list():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    term = Term(
        standard_name="登录模块",
        aliases=[],
        term_type="module",
        product_line="核心平台",
    )

    await client.sync_term(term)

    assert session.last_parameters["aliases"] == []


async def test_sync_terms_syncs_every_term_in_the_list():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))
    terms = [
        Term(standard_name="错误码E502", aliases=["网关超时"], term_type="error_code", product_line="核心平台"),
        Term(standard_name="登录模块", aliases=["认证模块"], term_type="module", product_line="核心平台"),
    ]

    await client.sync_terms(terms)

    assert len(session.calls) == 2
    synced_names = {call[1]["standard_name"] for call in session.calls}
    assert synced_names == {"错误码E502", "登录模块"}


def test_allowed_relation_types_include_all_ten_generic_types_and_not_the_old_one():
    from app.graphrag.neo4j_client import _ALLOWED_RELATION_TYPES

    assert _ALLOWED_RELATION_TYPES == {
        "RELATED_TO", "PART_OF", "IS_A", "REQUIRES", "ALTERNATIVE_TO",
        "CAUSES", "ADDRESSED_BY", "LOCATED_IN", "APPLIES_TO", "PRECEDES",
    }
    assert "BELONGS_TO_MODULE" not in _ALLOWED_RELATION_TYPES


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


async def test_merge_relation_rejects_the_retired_belongs_to_module_type():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    try:
        await client.merge_relation(
            subject_standard_name="a",
            object_standard_name="b",
            relation_type="BELONGS_TO_MODULE",
            source="a.md",
            tenant_id="t1",
            provenance="auto_merged",
            recorded_at=_NOW,
        )
        assert False, "BELONGS_TO_MODULE 已经被 PART_OF 取代，应该拒绝"
    except ValueError:
        pass


async def test_query_subgraph_sends_two_hop_union_query_for_chain_relations():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.query_subgraph("错误码E502", tenant_id="t1")

    assert "UNION" in session.last_query
    assert "REQUIRES|PRECEDES|PART_OF*2..2" in session.last_query
    assert "ALL(rel IN r WHERE rel.tenant_id = $tenant_id)" in session.last_query
    assert "AND related <> t" in session.last_query
    assert session.last_parameters == {"standard_name": "错误码E502", "tenant_id": "t1"}


async def test_count_relation_edges_for_term_returns_edge_count():
    session = FakeSession(rows=[{"edge_count": 3}])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term("错误码E502")

    assert count == 3
    assert session.last_parameters == {"standard_name": "错误码E502"}
    assert "ALIAS_OF" in session.last_query


async def test_count_relation_edges_for_term_returns_zero_when_no_rows():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    count = await client.count_relation_edges_for_term("孤立术语")

    assert count == 0


async def test_rename_term_node_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.rename_term_node(old_name="错误码E502", new_name="错误码E502v2")

    assert session.last_parameters == {
        "old_name": "错误码E502",
        "new_name": "错误码E502v2",
    }
    assert "MATCH" in session.last_query
    assert "SET t.standard_name = $new_name" in session.last_query
    # 必须是 MATCH+SET 原地改属性，不能是先删再建——删了再建会让节点
    # 已有的关系边找不到挂载对象，变成孤儿边
    assert "DELETE" not in session.last_query
    assert "CREATE" not in session.last_query


async def test_delete_term_node_sends_detach_delete_query():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.delete_term_node("废弃术语")

    assert session.last_parameters == {"standard_name": "废弃术语"}
    assert "DETACH DELETE" in session.last_query
