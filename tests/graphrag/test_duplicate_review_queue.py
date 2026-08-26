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
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema
from app.graphrag.terms_store import create_term, ensure_terms_schema, get_term, list_terms


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

    assert len(fake_store.update_calls) == 2
    # Call 0: tombstone the merged term
    tombstone_call = fake_store.update_calls[0]
    assert tombstone_call["standard_name"] == "可口可乐"
    assert tombstone_call["new_standard_name"].startswith("[已合并] ")
    assert tombstone_call["aliases"] == []
    assert tombstone_call["term_type"] == "公司"
    # Call 1: append onto keeper
    append_call = fake_store.update_calls[1]
    assert append_call["standard_name"] == "Coca-Cola"
    assert append_call["new_standard_name"] == "Coca-Cola"
    assert append_call["term_type"] == "公司"
    assert set(append_call["aliases"]) == {"coke", "可口可乐", "可乐公司"}
    assert await count_pending_duplicate_suggestions(conn, tenant_id="t1") == 0


async def test_approve_duplicate_suggestion_with_real_terms_store():
    """Integration test using real terms_store (not fake) to verify the tombstone
    approach works against the real _check_name_conflict validation."""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await create_term_type(conn, tenant_id="default", value="公司")
    await confirm_ontology(conn, "default")
    await ensure_duplicate_review_schema(conn)

    # Create the real terms (same pair as the fake test)
    await create_term(
        conn, tenant_id="default", standard_name="Coca-Cola",
        aliases=["coke"], term_type="公司",
    )
    await create_term(
        conn, tenant_id="default", standard_name="可口可乐",
        aliases=["可乐公司"], term_type="公司",
    )

    # Enqueue them as duplicates and approve, using the real terms_store (no fake)
    coca_cola_node_key = "公司:Coca-Cola"
    cola_node_key = "公司:可口可乐"
    await enqueue_duplicate_suggestion(
        conn, tenant_id="default",
        candidate_a_node_key=coca_cola_node_key,
        candidate_b_node_key=cola_node_key,
        similarity_score=0.8, reason="别名匹配",
    )
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="default")
    review_id = pending[0]["review_id"]

    # This should NOT raise TermNameConflictError
    await approve_duplicate_suggestion(
        conn, review_id=review_id, tenant_id="default",
        keep_node_key=coca_cola_node_key,
    )

    # Verify keeper's aliases now include the merged term's original standard_name and aliases
    keeper = await get_term(conn, "default", "Coca-Cola")
    assert set(keeper.aliases) == {"coke", "可口可乐", "可乐公司"}

    # Verify merged term still exists (not deleted) but is tombstoned
    all_terms = await list_terms(conn, tenant_id="default")
    merged_term_row = next(t for t in all_terms if t.node_key == cola_node_key)
    assert merged_term_row.standard_name.startswith("[已合并] ")
    assert merged_term_row.aliases == []

    await conn.close()


async def test_approve_duplicate_suggestion_compensates_on_append_failure(conn):
    """If the append step (step 2) fails after tombstone (step 1) succeeds,
    the merged term must be restored to its original state before the exception
    propagates, so the review stays pending and retry can work correctly."""
    from app.graphrag.ontology import Term
    from app.graphrag.terms_store import TermNameConflictError

    class _FailOnSecondUpdateStore:
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
                "aliases": aliases, "term_type": term_type,
            })
            # Raise on the second call (the append/step-2 call)
            if len(self.update_calls) == 2:
                raise TermNameConflictError("simulated name conflict on append")

    fake_store = _FailOnSecondUpdateStore()
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="公司:Coca-Cola",
        candidate_b_node_key="公司:可口可乐", similarity_score=0.8, reason="别名匹配",
    )
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    review_id = pending[0]["review_id"]

    # Approve should raise due to fake failure on step 2
    with pytest.raises(TermNameConflictError):
        await approve_duplicate_suggestion(
            conn, review_id=review_id, tenant_id="t1",
            keep_node_key="公司:Coca-Cola", terms_module=fake_store,
        )

    # Verify 3 calls were made: tombstone, failed append, and compensating restore
    assert len(fake_store.update_calls) == 3
    # Call 0: tombstone
    assert fake_store.update_calls[0]["standard_name"] == "可口可乐"
    assert fake_store.update_calls[0]["new_standard_name"].startswith("[已合并] ")
    assert fake_store.update_calls[0]["aliases"] == []
    # Call 1: attempted append (raised)
    assert fake_store.update_calls[1]["standard_name"] == "Coca-Cola"
    assert fake_store.update_calls[1]["new_standard_name"] == "Coca-Cola"
    # Call 2: compensating restore
    assert fake_store.update_calls[2]["standard_name"] == f"[已合并] 公司:可口可乐"
    assert fake_store.update_calls[2]["new_standard_name"] == "可口可乐"
    assert fake_store.update_calls[2]["aliases"] == ["可乐公司"]

    # Verify the review row is still pending (not marked approved)
    pending_after = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    assert len(pending_after) == 1
    assert pending_after[0]["review_id"] == review_id


async def test_approve_duplicate_suggestion_reraises_original_error_when_compensation_also_fails(
    conn, caplog
):
    """Fix 3：补偿恢复（把被合并术语的 standard_name/aliases 改回合并前的
    状态）本身也可能失败（比如并发写入已经把这个名字抢回去了）——这种情况
    下不能让补偿失败的异常吞掉/替换调用方原本需要看到的错误：真正触发
    这整条回滚路径的是"追加别名到保留术语"这一步的失败（append_exc），
    调用方关心的是这个。补偿失败时改成记一条 ERROR 日志，带上 tenant_id、
    被合并术语的 node_key、丢失的原始 standard_name/aliases，以及两个异常，
    留下人工核对/手动恢复的线索，然后依然重新抛出原始异常。"""
    from app.graphrag.ontology import Term
    from app.graphrag.terms_store import TermNameConflictError

    class _FailOnSecondAndThirdUpdateStore:
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
                "aliases": aliases, "term_type": term_type,
            })
            if len(self.update_calls) == 2:
                raise TermNameConflictError("simulated append conflict")
            if len(self.update_calls) == 3:
                raise RuntimeError("simulated concurrent write stole the name back")

    fake_store = _FailOnSecondAndThirdUpdateStore()
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="公司:Coca-Cola",
        candidate_b_node_key="公司:可口可乐", similarity_score=0.8, reason="别名匹配",
    )
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    review_id = pending[0]["review_id"]

    with caplog.at_level("ERROR", logger="app.graphrag.duplicate_review_queue"):
        with pytest.raises(TermNameConflictError, match="simulated append conflict"):
            await approve_duplicate_suggestion(
                conn, review_id=review_id, tenant_id="t1",
                keep_node_key="公司:Coca-Cola", terms_module=fake_store,
            )

    # 三次调用：tombstone、失败的追加、失败的补偿恢复
    assert len(fake_store.update_calls) == 3

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    # 丢失数据的排查线索必须都在这条日志里：租户、被合并术语的原始名字/
    # 别名、两个异常各自的信息。
    assert "t1" in message
    assert "可口可乐" in message
    assert "可乐公司" in message
    assert "simulated append conflict" in message
    assert "simulated concurrent write stole the name back" in message

    # 待审核记录仍然是 pending——没有被误标记成 approved。
    pending_after = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    assert len(pending_after) == 1
    assert pending_after[0]["review_id"] == review_id


async def test_approve_unknown_review_id_raises(conn):
    with pytest.raises(DuplicateReviewNotFoundError):
        await approve_duplicate_suggestion(
            conn, review_id=999, tenant_id="t1", keep_node_key="a"
        )
