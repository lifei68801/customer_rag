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


async def test_approve_duplicate_suggestion_merges_via_real_terms_store_and_marks_approved():
    """approve_duplicate_suggestion 现在只负责队列自己的部分（校验
    keep_node_key、判断谁是 merged、把状态改成 approved），真正的合并写入
    委托给 terms_store.merge_terms()——这条测试用真实的 terms_store（不是
    fake）验证两边接起来确实工作：别名被合并、被合并那条被墓碑化、且审核
    记录本身被正确标记成 approved（合并逻辑本身的墓碑格式/补偿回滚细节，
    已经在 test_terms_store.py 里针对 merge_terms 本身覆盖过，这里不重复
    测）。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await create_term_type(conn, tenant_id="default", value="公司")
    await confirm_ontology(conn, "default")
    await ensure_duplicate_review_schema(conn)

    await create_term(
        conn, tenant_id="default", standard_name="Coca-Cola",
        aliases=["coke"], term_type="公司",
    )
    await create_term(
        conn, tenant_id="default", standard_name="可口可乐",
        aliases=["可乐公司"], term_type="公司",
    )

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

    await approve_duplicate_suggestion(
        conn, review_id=review_id, tenant_id="default",
        keep_node_key=coca_cola_node_key,
    )

    keeper = await get_term(conn, "default", "Coca-Cola")
    assert set(keeper.aliases) == {"coke", "可口可乐", "可乐公司"}

    all_terms = await list_terms(conn, tenant_id="default")
    merged_term_row = next(t for t in all_terms if t.node_key == cola_node_key)
    assert merged_term_row.standard_name.startswith("[已合并] ")
    assert merged_term_row.aliases == []

    assert await count_pending_duplicate_suggestions(conn, tenant_id="default") == 0

    await conn.close()


async def test_approve_duplicate_suggestion_propagates_conflict_and_leaves_row_pending():
    """merge_terms() 的追加步骤在真实场景下也可能撞上一个真的 TermNameConflictError
    （比如旁观术语已经持有了被合并那条要追加的别名）——这里不重复构造 merge_terms
    自己那套墓碑/补偿细节（已经在 test_terms_store.py 覆盖），只验证队列这一层
    正确地把异常原样透传给调用方，并且没有把审核记录误标记成 approved。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_ontology_schema(conn)
    await create_term_type(conn, tenant_id="default", value="公司")
    await confirm_ontology(conn, "default")
    await ensure_duplicate_review_schema(conn)

    await create_term(
        conn, tenant_id="default", standard_name="Coca-Cola",
        aliases=[], term_type="公司",
    )
    await create_term(
        conn, tenant_id="default", standard_name="可口可乐",
        aliases=["可乐"], term_type="公司",
    )
    # 旁观术语已经持有了被合并那条唯一的别名"可乐"——这种状态用 create_term()
    # 走正常路径创建不出来（_check_name_conflict 会在第二条创建时就拦下来），
    # 直接插行绕开验证，模拟 ETL 写入路径产生的真实冲突状态。
    await conn.execute(
        "INSERT INTO terms (tenant_id, node_key, standard_name, aliases, term_type, "
        "extra_properties, source) VALUES (?, ?, ?, ?, ?, '{}', 'manual')",
        ("default", "公司:某第三方公司", "某第三方公司", '["可乐"]', "公司"),
    )
    await conn.commit()

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

    from app.graphrag.terms_store import TermNameConflictError

    with pytest.raises(TermNameConflictError):
        await approve_duplicate_suggestion(
            conn, review_id=review_id, tenant_id="default",
            keep_node_key=coca_cola_node_key,
        )

    pending_after = await list_pending_duplicate_suggestions(conn, tenant_id="default")
    assert len(pending_after) == 1
    assert pending_after[0]["review_id"] == review_id

    await conn.close()


async def test_approve_duplicate_suggestion_candidate_term_not_found_translates_to_duplicate_review_not_found_error(
    conn,
):
    """审核记录里的候选 node_key 指向的术语已经不存在了（比如被别的操作
    删除）——merge_terms() 会抛 terms_store.TermNotFoundError，队列这一层
    要把它翻译成自己的 DuplicateReviewNotFoundError（路由层只认识队列自己
    的异常类型，不直接依赖 terms_store 的异常）。"""
    await ensure_terms_schema(conn)
    await enqueue_duplicate_suggestion(
        conn, tenant_id="t1", candidate_a_node_key="公司:Coca-Cola",
        candidate_b_node_key="公司:不存在的术语", similarity_score=0.8, reason="别名匹配",
    )
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    review_id = pending[0]["review_id"]

    with pytest.raises(DuplicateReviewNotFoundError):
        await approve_duplicate_suggestion(
            conn, review_id=review_id, tenant_id="t1",
            keep_node_key="公司:Coca-Cola",
        )

    # 待审核记录仍然是 pending——没有被误标记成 approved。
    pending_after = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    assert len(pending_after) == 1
    assert pending_after[0]["review_id"] == review_id


async def test_approve_unknown_review_id_raises(conn):
    with pytest.raises(DuplicateReviewNotFoundError):
        await approve_duplicate_suggestion(
            conn, review_id=999, tenant_id="t1", keep_node_key="a"
        )
