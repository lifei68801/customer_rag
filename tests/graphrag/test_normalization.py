import aiosqlite

from app.graphrag.normalization import normalize_and_write_relations
from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema, list_pending_reviews

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
        product_line="核心平台",
    ),
    Term(
        standard_name="登录模块",
        aliases=["认证模块"],
        term_type="module",
        product_line="核心平台",
    ),
]


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.deleted_sources: list[str] = []

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type, source
    ) -> None:
        if relation_type not in {"RELATED_TO", "BELONGS_TO_MODULE"}:
            raise ValueError("不允许的关系类型")
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
            }
        )

    async def delete_relations_by_source(self, source: str) -> None:
        self.deleted_sources.append(source)


async def test_writes_relation_when_both_sides_resolve_via_alias():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"}
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md"
    )

    assert written == 1
    assert graph_client.written == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
            "source": "a.md",
        }
    ]


async def test_drops_relation_when_one_side_unresolved():
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md"
    )

    assert written == 0
    assert graph_client.written == []


async def test_drops_relation_with_invalid_relation_type_without_crashing_batch():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"},
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"},
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md"
    )

    assert written == 1


async def test_enqueues_unresolved_candidate_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn)
    assert len(pending) == 1
    assert pending[0]["subject_candidate"] == "网关超时"
    assert pending[0]["object_candidate"] == "不存在的实体"
    assert pending[0]["reason"] == "object_unresolved"


async def test_enqueues_invalid_relation_type_for_review_when_review_conn_provided():
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"}
    ]

    written = await normalize_and_write_relations(
        relations,
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        review_conn=review_conn,
    )

    assert written == 0
    pending = await list_pending_reviews(review_conn)
    assert len(pending) == 1
    assert pending[0]["reason"] == "invalid_relation_type"


async def test_does_not_enqueue_when_review_conn_not_provided():
    """默认行为保持不变：不传 review_conn 时仍然只是丢弃+记日志，不建表不写库。"""
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client, source="a.md"
    )

    assert written == 0
