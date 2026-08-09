from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term


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
    assert "tenant_id" in session.last_query


async def test_merge_relation_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_standard_name="错误码E502",
        object_standard_name="登录模块",
        relation_type="RELATED_TO",
        source="a.md",
        tenant_id="t1",
    )

    assert session.last_parameters == {
        "subject_name": "错误码E502",
        "object_name": "登录模块",
        "source": "a.md",
        "tenant_id": "t1",
    }
    assert "RELATED_TO" in session.last_query
    assert "MERGE" in session.last_query
    assert "tenant_id" in session.last_query


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
    assert session.last_parameters == {"standard_name": "错误码E502", "tenant_id": "t1"}
