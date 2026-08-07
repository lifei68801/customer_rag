import aiosqlite
import pytest

from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    approve_review,
    enqueue_for_review,
    ensure_review_schema,
    list_pending_reviews,
    reject_review,
)


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    return conn


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
            }
        )


async def test_enqueue_then_list_pending_returns_the_candidate():
    conn = await _connect()

    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
    )

    pending = await list_pending_reviews(conn)
    assert len(pending) == 1
    assert pending[0]["review_id"] == review_id
    assert pending[0]["subject_candidate"] == "网关超时示例2.0"
    assert pending[0]["reason"] == "subject_unresolved"


async def test_approve_review_writes_relation_and_removes_from_pending():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
    )

    await approve_review(
        conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        graph_client=graph_client,
    )

    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
        }
    ]
    assert await list_pending_reviews(conn) == []


async def test_reject_review_removes_from_pending_without_writing():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="不存在的东西",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
    )

    await reject_review(conn, review_id=review_id, note="确认是噪声，非真实实体")

    assert await list_pending_reviews(conn) == []
    assert graph_client.written == []


async def test_approve_unknown_review_id_raises():
    conn = await _connect()
    graph_client = FakeGraphClient()

    with pytest.raises(ReviewNotFoundError):
        await approve_review(
            conn,
            review_id=999,
            subject_standard_name="a",
            object_standard_name="b",
            graph_client=graph_client,
        )


async def test_approve_already_resolved_review_raises():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="x",
        object_candidate="y",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
    )
    await reject_review(conn, review_id=review_id)

    with pytest.raises(ReviewAlreadyResolvedError):
        await approve_review(
            conn,
            review_id=review_id,
            subject_standard_name="a",
            object_standard_name="b",
            graph_client=graph_client,
        )


async def test_enqueue_with_suggested_names_is_returned_by_list_pending():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="网关超时了",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="fuzzy_match_needs_confirmation",
        suggested_subject_standard_name="错误码E502",
        suggested_object_standard_name=None,
    )

    pending = await list_pending_reviews(conn)
    assert len(pending) == 1
    assert pending[0]["suggested_subject_standard_name"] == "错误码E502"
    assert pending[0]["suggested_object_standard_name"] is None


async def test_enqueue_without_suggested_names_defaults_to_null():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="不存在的东西",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
    )

    pending = await list_pending_reviews(conn)
    assert len(pending) == 1
    assert pending[0]["suggested_subject_standard_name"] is None
    assert pending[0]["suggested_object_standard_name"] is None
