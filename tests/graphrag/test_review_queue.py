from datetime import datetime

import aiosqlite
import pytest

from app.graphrag.ontology import Term
from app.graphrag.review_queue import (
    ReviewAlreadyResolvedError,
    ReviewNotFoundError,
    StandardNameNotInTermsError,
    approve_review,
    count_pending_reviews,
    count_resolved_reviews,
    enqueue_for_review,
    ensure_review_schema,
    list_pending_reviews,
    list_resolved_reviews,
    reject_review,
)

_NOW = datetime(2026, 8, 12, 12, 0, 0)


def _terms(*standard_names: str, tenant_id: str = "t1") -> list[Term]:
    """构造术语列表，默认 node_key == standard_name（未改名场景）。"""
    return [
        Term(
            tenant_id=tenant_id,
            node_key=name,
            standard_name=name,
            aliases=[],
            term_type="",
            product_line="",
        )
        for name in standard_names
    ]


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    return conn


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
        provenance,
        recorded_at,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
                "provenance": provenance,
                "recorded_at": recorded_at,
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
        source="faq.md",
        tenant_id="t1",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["review_id"] == review_id
    assert pending[0]["subject_candidate"] == "网关超时示例2.0"
    assert pending[0]["reason"] == "subject_unresolved"
    assert pending[0]["source"] == "faq.md"


async def test_list_pending_reviews_does_not_leak_another_tenants_rows():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    assert await list_pending_reviews(conn, tenant_id="t2") == []


async def test_approve_review_writes_relation_with_source_and_tenant_and_removes_from_pending():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="faq.md",
        tenant_id="t1",
    )

    await approve_review(
        conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
        terms=_terms("示例错误码E502", "示例登录模块"),
        now=_NOW,
    )

    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": "faq.md",
            "tenant_id": "t1",
            "provenance": "human_approved",
            "recorded_at": _NOW,
        }
    ]
    assert await list_pending_reviews(conn, tenant_id="t1") == []


async def test_approve_review_writes_node_key_not_standard_name_after_term_was_renamed():
    """Fix 2 回归测试：merge_relation 现在按 {tenant_id, node_key} MERGE
    端点节点（node_key 是创建时固定的身份键，改名后不变——ADR-0003）。
    这里构造"已改名"的术语——node_key（创建时的原始值）与人工确认时
    传入的当前 standard_name 不同——验证 approve_review 传给
    merge_relation 的是术语的 node_key，而不是审核员直接输入的展示名，
    否则改名后批准会在 Neo4j 里 MERGE 出一个没有 standard_name 属性的
    幽灵节点，而不是命中真实节点。
    """
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="faq.md",
        tenant_id="t1",
    )
    renamed_terms = [
        Term(
            tenant_id="t1",
            node_key="示例错误码E502_原始名",
            standard_name="示例错误码E502",
            aliases=[],
            term_type="",
            product_line="",
        ),
        Term(
            tenant_id="t1",
            node_key="示例登录模块_原始名",
            standard_name="示例登录模块",
            aliases=[],
            term_type="",
            product_line="",
        ),
    ]

    await approve_review(
        conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
        terms=renamed_terms,
        now=_NOW,
    )

    assert graph_client.written[0]["subject"] == "示例错误码E502_原始名"
    assert graph_client.written[0]["object"] == "示例登录模块_原始名"


async def test_approve_review_from_wrong_tenant_raises_not_found():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    with pytest.raises(ReviewNotFoundError):
        await approve_review(
            conn, review_id=review_id, subject_standard_name="x",
            object_standard_name="y", tenant_id="t2", graph_client=graph_client,
            terms=[], now=_NOW,
        )
    assert graph_client.written == []


async def test_reject_review_from_wrong_tenant_raises_not_found():
    conn = await _connect()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    with pytest.raises(ReviewNotFoundError):
        await reject_review(conn, review_id=review_id, tenant_id="t2")

    # 记录还在 t1 的待审核队列里，没有被 t2 的驳回请求误处理掉
    assert len(await list_pending_reviews(conn, tenant_id="t1")) == 1


async def test_reject_review_removes_from_pending_without_writing():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="不存在的东西",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    await reject_review(conn, review_id=review_id, tenant_id="t1", note="确认是噪声，非真实实体")

    assert await list_pending_reviews(conn, tenant_id="t1") == []
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
            tenant_id="t1",
            graph_client=graph_client,
            terms=[],
            now=_NOW,
        )


async def test_approve_already_resolved_review_raises():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn, subject_candidate="x", object_candidate="y", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await reject_review(conn, review_id=review_id, tenant_id="t1")

    with pytest.raises(ReviewAlreadyResolvedError):
        await approve_review(
            conn,
            review_id=review_id,
            subject_standard_name="a",
            object_standard_name="b",
            tenant_id="t1",
            graph_client=graph_client,
            terms=[],
            now=_NOW,
        )


async def test_enqueue_with_suggested_names_is_returned_by_list_pending():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="网关超时了",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="fuzzy_match_needs_confirmation",
        source="s.md",
        tenant_id="t1",
        suggested_subject_standard_name="错误码E502",
        suggested_object_standard_name=None,
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
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
        source="s.md",
        tenant_id="t1",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["suggested_subject_standard_name"] is None
    assert pending[0]["suggested_object_standard_name"] is None


async def test_list_resolved_reviews_returns_approved_and_rejected_ordered_by_resolved_at():
    conn = await _connect()
    graph_client = FakeGraphClient()
    approved_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    rejected_id = await enqueue_for_review(
        conn, subject_candidate="c", object_candidate="d", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await approve_review(
        conn, review_id=approved_id, subject_standard_name="A", object_standard_name="B",
        tenant_id="t1", graph_client=graph_client, terms=_terms("A", "B"), now=_NOW,
    )
    await reject_review(conn, review_id=rejected_id, tenant_id="t1", note="噪声")

    resolved = await list_resolved_reviews(conn, tenant_id="t1")
    assert {r["review_id"] for r in resolved} == {approved_id, rejected_id}

    only_approved = await list_resolved_reviews(conn, tenant_id="t1", status="approved")
    assert [r["review_id"] for r in only_approved] == [approved_id]


async def test_list_resolved_reviews_does_not_include_pending():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    assert await list_resolved_reviews(conn, tenant_id="t1") == []


async def test_list_pending_reviews_respects_limit_and_offset():
    conn = await _connect()
    for i in range(5):
        await enqueue_for_review(
            conn, subject_candidate=f"s{i}", object_candidate=f"o{i}", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )

    page1 = await list_pending_reviews(conn, tenant_id="t1", limit=2, offset=0)
    page2 = await list_pending_reviews(conn, tenant_id="t1", limit=2, offset=2)

    assert [r["subject_candidate"] for r in page1] == ["s0", "s1"]
    assert [r["subject_candidate"] for r in page2] == ["s2", "s3"]


async def test_list_pending_reviews_without_limit_returns_everything():
    conn = await _connect()
    for i in range(3):
        await enqueue_for_review(
            conn, subject_candidate=f"s{i}", object_candidate=f"o{i}", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )

    pending = await list_pending_reviews(conn, tenant_id="t1")

    assert len(pending) == 3


async def test_count_pending_reviews_matches_tenant_scoped_total():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await enqueue_for_review(
        conn, subject_candidate="c", object_candidate="d", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await enqueue_for_review(
        conn, subject_candidate="e", object_candidate="f", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t2",
    )

    assert await count_pending_reviews(conn, tenant_id="t1") == 2
    assert await count_pending_reviews(conn, tenant_id="t2") == 1


async def test_count_resolved_reviews_matches_status_filter():
    conn = await _connect()
    graph_client = FakeGraphClient()
    approved_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    rejected_id = await enqueue_for_review(
        conn, subject_candidate="c", object_candidate="d", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )
    await approve_review(
        conn, review_id=approved_id, subject_standard_name="A", object_standard_name="B",
        tenant_id="t1", graph_client=graph_client, terms=_terms("A", "B"), now=_NOW,
    )
    await reject_review(conn, review_id=rejected_id, tenant_id="t1", note="噪声")

    assert await count_resolved_reviews(conn, tenant_id="t1") == 2
    assert await count_resolved_reviews(conn, tenant_id="t1", status="approved") == 1
    assert await count_resolved_reviews(conn, tenant_id="t1", status="rejected") == 1


async def test_list_resolved_reviews_breaks_resolved_at_ties_by_review_id_desc():
    conn = await _connect()
    ids = []
    for i in range(3):
        review_id = await enqueue_for_review(
            conn, subject_candidate=f"s{i}", object_candidate=f"o{i}", relation_type="RELATED_TO",
            reason="subject_unresolved", source="s.md", tenant_id="t1",
        )
        ids.append(review_id)
    for review_id in ids:
        await reject_review(conn, review_id=review_id, tenant_id="t1")

    resolved = await list_resolved_reviews(conn, tenant_id="t1")

    # 同一秒内驳回的三条记录 resolved_at 完全相同，必须靠 review_id 兜底
    # 排序，保证结果稳定——不然分页边界处会出现同一条记录跨页重复、或者
    # 彻底消失的问题。
    assert [r["review_id"] for r in resolved] == list(reversed(ids))


async def test_enqueue_with_evidence_is_returned_by_list_pending():
    conn = await _connect()

    await enqueue_for_review(
        conn,
        subject_candidate="网关超时",
        object_candidate="登录模块",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
        evidence="文档提到网关超时会影响登录模块",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert pending[0]["evidence"] == "文档提到网关超时会影响登录模块"


async def test_enqueue_without_evidence_defaults_to_empty_string():
    conn = await _connect()

    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert pending[0]["evidence"] == ""


async def test_list_resolved_reviews_includes_evidence():
    conn = await _connect()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
        evidence="原文引用示例",
    )
    await reject_review(conn, review_id=review_id, tenant_id="t1")

    resolved = await list_resolved_reviews(conn, tenant_id="t1")
    assert resolved[0]["evidence"] == "原文引用示例"


async def test_approve_review_rejects_standard_name_not_in_terms():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    with pytest.raises(StandardNameNotInTermsError):
        await approve_review(
            conn,
            review_id=review_id,
            subject_standard_name="不在术语表里的名字",
            object_standard_name="示例登录模块",
            tenant_id="t1",
            graph_client=graph_client,
            terms=_terms("示例登录模块"),
            now=_NOW,
        )

    # 校验失败不写图谱、也不改变 review 状态——还留在待审核队列里，
    # 方便审核员改对了标准名之后重新提交
    assert graph_client.written == []
    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["review_id"] == review_id


async def test_approve_review_rejects_all_names_when_terms_list_is_empty():
    conn = await _connect()
    graph_client = FakeGraphClient()
    review_id = await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    with pytest.raises(StandardNameNotInTermsError):
        await approve_review(
            conn,
            review_id=review_id,
            subject_standard_name="随便什么名字",
            object_standard_name="随便什么名字",
            tenant_id="t1",
            graph_client=graph_client,
            terms=[],
            now=_NOW,
        )

    assert graph_client.written == []
    pending = await list_pending_reviews(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["review_id"] == review_id


async def test_enqueue_for_review_stores_type_candidates():
    conn = await _connect()

    review_id = await enqueue_for_review(
        conn, subject_candidate="错误码E509", object_candidate="重启路由器",
        relation_type="ADDRESSED_BY", reason="subject_unresolved",
        source="a.md", tenant_id="t1",
        subject_type_candidate="error_code", object_type_candidate="solution",
    )
    pending = await list_pending_reviews(conn, tenant_id="t1")
    row = next(r for r in pending if r["review_id"] == review_id)
    assert row["subject_type_candidate"] == "error_code"
    assert row["object_type_candidate"] == "solution"
