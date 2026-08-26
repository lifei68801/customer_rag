import aiosqlite
import pytest

from app.graphrag.duplicate_review_queue import (
    DuplicateReviewAlreadyResolvedError,
    DuplicateReviewNotFoundError,
    approve_duplicate_suggestion,
    count_pending_duplicate_suggestions,
    ensure_duplicate_review_schema,
    enqueue_duplicate_suggestion,
    has_any_duplicate_record,
    list_pending_duplicate_suggestions,
    reject_duplicate_suggestion,
)


@pytest.fixture
async def conn():
    async with aiosqlite.connect(":memory:") as conn:
        await ensure_duplicate_review_schema(conn)
        yield conn


async def test_enqueue_and_list_pending(conn):
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="公司:Coca-Cola",
        candidate_b_node_key="公司:可口可乐", similarity_score=0.8, reason="别名匹配",
    )

    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")

    assert len(pending) == 1
    assert pending[0]["candidate_a_node_key"] == "公司:Coca-Cola"
    assert pending[0]["candidate_b_node_key"] == "公司:可口可乐"
    assert pending[0]["similarity_score"] == 0.8
    assert pending[0]["status"] == "pending"


async def test_enqueue_duplicate_pair_is_idempotent(conn):
    """同一对候选（tenant_id + 两个 node_key）重复入队，只应该有一条 pending 记录——
    对应 idx_duplicate_review_queue_pair 唯一索引，批跑 worker 重复调用不产生
    重复建议。"""
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b",
        similarity_score=0.7, reason="r1",
    )
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b",
        similarity_score=0.7, reason="r1",
    )

    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    assert len(pending) == 1


async def test_count_pending_duplicate_suggestions(conn):
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b",
        similarity_score=0.7, reason="r",
    )
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="c", candidate_b_node_key="d",
        similarity_score=0.7, reason="r",
    )

    assert await count_pending_duplicate_suggestions(conn, tenant_id="t1") == 2


async def test_has_any_duplicate_record_is_order_insensitive(conn):
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b",
        similarity_score=0.7, reason="r",
    )

    assert await has_any_duplicate_record(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b"
    )
    assert await has_any_duplicate_record(
        conn, tenant_id="t1", candidate_a_node_key="b", candidate_b_node_key="a"
    )
    assert not await has_any_duplicate_record(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="c"
    )


async def test_reject_duplicate_suggestion_marks_rejected_and_has_any_still_true(conn):
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b",
        similarity_score=0.7, reason="r",
    )
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    review_id = pending[0]["review_id"]

    await reject_duplicate_suggestion(conn, review_id=review_id, tenant_id="t1", note="不是同一个实体")

    assert await count_pending_duplicate_suggestions(conn, tenant_id="t1") == 0
    # 驳回后这一对依然"有记录"（status='rejected'），批跑 worker 据此跳过，不再重新入队
    assert await has_any_duplicate_record(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b"
    )


async def test_reject_unknown_review_id_raises(conn):
    with pytest.raises(DuplicateReviewNotFoundError):
        await reject_duplicate_suggestion(conn, review_id=999, tenant_id="t1")


async def test_reject_already_resolved_raises(conn):
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="a", candidate_b_node_key="b",
        similarity_score=0.7, reason="r",
    )
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    review_id = pending[0]["review_id"]
    await reject_duplicate_suggestion(conn, review_id=review_id, tenant_id="t1")

    with pytest.raises(DuplicateReviewAlreadyResolvedError):
        await reject_duplicate_suggestion(conn, review_id=review_id, tenant_id="t1")


async def test_approve_duplicate_suggestion_merges_aliases(conn):
    """approve 时用 keep_node_key 定位保留哪一条，被合并那条的 standard_name
    连同它自己已有的 aliases 全部追加进保留那条的 aliases（去重）——不是只
    追加 standard_name，否则被合并那条自己的别名会变成孤儿，resolve_term()
    再也找不回它们。"""
    from app.graphrag.ontology import Term

    class _FakeTermsStore:
        def __init__(self):
            self.terms = {
                "公司:Coca-Cola": Term(
                    tenant_id="t1", node_key="公司:Coca-Cola", standard_name="Coca-Cola",
                    aliases=["coke"], term_type="公司",
                ),
                "公司:可口可乐": Term(
                    tenant_id="t1", node_key="公司:可口可乐", standard_name="可口可乐",
                    aliases=["可乐公司"], term_type="公司",
                ),
            }
            self.update_calls = []

        async def list_terms(self, conn, tenant_id):
            return list(self.terms.values())

        async def update_term(self, conn, *, tenant_id, standard_name, new_standard_name,
                               aliases, term_type, extra_properties=None, current_term_type=None):
            self.update_calls.append({
                "standard_name": standard_name, "new_standard_name": new_standard_name,
                "aliases": aliases, "term_type": term_type, "current_term_type": current_term_type,
            })

    fake_store = _FakeTermsStore()
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="公司:Coca-Cola",
        candidate_b_node_key="公司:可口可乐", similarity_score=0.8, reason="别名匹配",
    )
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    review_id = pending[0]["review_id"]

    await approve_duplicate_suggestion(
        conn, review_id=review_id, tenant_id="t1",
        keep_node_key="公司:Coca-Cola", terms_module=fake_store,
    )

    assert len(fake_store.update_calls) == 1
    call = fake_store.update_calls[0]
    assert call["standard_name"] == "Coca-Cola"
    assert call["new_standard_name"] == "Coca-Cola"
    assert call["term_type"] == "公司"
    assert set(call["aliases"]) == {"coke", "可口可乐", "可乐公司"}
    assert await count_pending_duplicate_suggestions(conn, tenant_id="t1") == 0


async def test_approve_unknown_review_id_raises(conn):
    with pytest.raises(DuplicateReviewNotFoundError):
        await approve_duplicate_suggestion(
            conn, review_id=999, tenant_id="t1", keep_node_key="a"
        )
