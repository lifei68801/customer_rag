from app.graphrag.neo4j_client import Neo4jGraphClient


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

    async def run(self, query: str, parameters: dict | None = None) -> FakeResult:
        self.last_query = query
        self.last_parameters = parameters
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

    results = await client.query_subgraph("错误码E502")

    assert results == [{"related_name": "登录模块", "relation_type": "RELATED_TO"}]
    assert session.last_parameters == {"standard_name": "错误码E502"}


async def test_merge_relation_sends_expected_query_and_parameters():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    await client.merge_relation(
        subject_standard_name="错误码E502",
        object_standard_name="登录模块",
        relation_type="RELATED_TO",
    )

    assert session.last_parameters == {
        "subject_name": "错误码E502",
        "object_name": "登录模块",
    }
    assert "RELATED_TO" in session.last_query
    assert "MERGE" in session.last_query


async def test_merge_relation_rejects_unrecognized_relation_type():
    session = FakeSession(rows=[])
    client = Neo4jGraphClient(driver=FakeDriver(session))

    try:
        await client.merge_relation(
            subject_standard_name="a",
            object_standard_name="b",
            relation_type="DROP TABLE",
        )
        assert False, "应拒绝非法关系类型"
    except ValueError:
        pass
