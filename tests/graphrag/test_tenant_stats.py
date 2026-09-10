"""看板上「一个领域的四个数字」。

这个模块的全部价值在于四个数字各自来自正确的地方、各自只算这一个租户，
以及**图谱查不通时不报 0**。报 0 的话用户看到的是「这个领域一条关系都
没有」——一个看起来正常、实际是错的数字，他会据此以为数据没导进去，
然后去重跑一遍 ETL。
"""

from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import (
    checkout_draft,
    confirm_ontology,
    ensure_ontology_schema,
)
from app.graphrag.attribute_conflicts import (
    ensure_attribute_conflicts_schema,
    record_conflict,
)
from app.graphrag.duplicate_review_queue import (
    ensure_duplicate_review_schema,
    enqueue_duplicate_suggestion,
)
from app.graphrag.review_queue import ensure_review_schema, enqueue_for_review
from app.graphrag.tenant_personas_store import ensure_tenant_personas_schema
from app.graphrag.tenant_stats import TenantStats, collect_tenant_stats
from app.graphrag.term_edits_store import (
    FIELD_DELETED,
    ensure_term_edits_schema,
    upsert_term_edit,
)
from app.graphrag.terms_store import create_term, ensure_terms_schema
from app.ingestion.tracking import ensure_tracking_schema, record_ingested

pytestmark = pytest.mark.anyio


class FakeGraph:
    """按租户报告边数。没登记的租户返回 0。

    `broken` 里的租户抛异常——用来模拟图谱连不上，那是这个模块最要紧的
    一条分支。
    """

    def __init__(
        self, edges: dict[str, int] | None = None, *, broken: set[str] | None = None
    ) -> None:
        self._edges = edges or {}
        self._broken = broken or set()

    async def count_relation_edges_for_tenant(self, *, tenant_id: str) -> int:
        if tenant_id in self._broken:
            raise RuntimeError("Neo4j 连不上")
        return self._edges.get(tenant_id, 0)


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    await ensure_term_edits_schema(conn)
    await ensure_ontology_schema(conn)
    await ensure_review_schema(conn)
    await ensure_duplicate_review_schema(conn)
    await ensure_attribute_conflicts_schema(conn)
    # 看板也读数字人表（失效引导问题）。启动时 app/main.py 建这张表。
    await ensure_tenant_personas_schema(conn)
    for tenant_id in ("demo", "other"):
        # 分类要**已确认**才能拿来建实体：草稿态的分类 create_term 不认
        # （UnknownCategoryError）。三步一套是本仓库既有的写法。
        await checkout_draft(conn, tenant_id)
        await create_term_type(conn, tenant_id=tenant_id, value="产品", actor="alice")
        await confirm_ontology(conn, tenant_id, actor="alice")
    return conn


async def _ingestion_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    return conn


async def _seed_terms(conn: aiosqlite.Connection, tenant_id: str, count: int) -> None:
    for i in range(count):
        await create_term(
            conn, tenant_id=tenant_id, standard_name=f"P{i}", aliases=[], term_type="产品"
        )


async def _seed_documents(conn: aiosqlite.Connection, tenant_id: str, count: int) -> None:
    for i in range(count):
        await record_ingested(
            conn, tenant_id=tenant_id, file_path=f"{tenant_id}/doc{i}.md",
            content_hash=f"h{i}", chunk_count=1,
        )


async def _seed_pending_reviews(
    conn: aiosqlite.Connection, tenant_id: str, count: int
) -> None:
    for i in range(count):
        await enqueue_for_review(
            conn, subject_candidate=f"S{i}", object_candidate=f"O{i}",
            relation_type="RELATED_TO", reason="测试", source="t.md", tenant_id=tenant_id,
        )


async def _seed_duplicate_suggestions(
    conn: aiosqlite.Connection, tenant_id: str, count: int
) -> None:
    for i in range(count):
        await enqueue_duplicate_suggestion(
            conn, tenant_id=tenant_id, candidate_a_node_key=f"产品:A{i}",
            candidate_b_node_key=f"产品:B{i}", similarity_score=0.9, reason="测试",
        )


async def test_collects_all_four_numbers_from_the_right_sources():
    """四个数字互不相同：13 / 27 / 5 / 3。

    相同的话，把实体数接到关系数那一格的实现也能变绿。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_terms(review_conn, "demo", 13)
        await _seed_documents(ingestion_conn, "demo", 5)
        await _seed_pending_reviews(review_conn, "demo", 3)

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph({"demo": 27}), tenant_id="demo"
        )

        assert stats == TenantStats(
            tenant_id="demo",
            term_count=13,
            edge_count=27,
            document_count=5,
            pending_review_count=3,
            sheet_row_count=0,
            stale_question_count=0,
        )
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def test_each_number_is_scoped_to_this_tenant():
    """另一个租户的数据不能出现在这个租户的统计里。

    两个租户各自的四个数字都不同，且 other 的每一项都比 demo 大——漏了
    tenant_id 条件的实现会把两边加起来，那个和跟任何一边都对不上。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_terms(review_conn, "demo", 2)
        await _seed_documents(ingestion_conn, "demo", 1)
        await _seed_pending_reviews(review_conn, "demo", 4)
        await _seed_terms(review_conn, "other", 7)
        await _seed_documents(ingestion_conn, "other", 9)
        await _seed_pending_reviews(review_conn, "other", 11)

        graph = FakeGraph({"demo": 3, "other": 100})
        demo = await collect_tenant_stats(review_conn, ingestion_conn, graph, tenant_id="demo")
        other = await collect_tenant_stats(review_conn, ingestion_conn, graph, tenant_id="other")

        assert (demo.term_count, demo.document_count, demo.pending_review_count) == (2, 1, 4)
        assert (other.term_count, other.document_count, other.pending_review_count) == (7, 9, 11)
        assert demo.edge_count == 3
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def test_term_count_follows_the_merged_view_not_the_raw_table():
    """实体数要等于用户在实体明细页数得出来的条数。

    人工删除（__deleted__）的行仍留在 terms 原始表里，但不出现在列表中。
    用 count_terms（裸表）的话，看板说 3 个实体、实体明细页只列得出 2 个
    ——用户会以为有一个实体"丢了"，然后去查一个根本不存在的问题。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_terms(review_conn, "demo", 3)
        await upsert_term_edit(
            review_conn, tenant_id="demo", node_key="产品:P0",
            field=FIELD_DELETED, value="1", edited_by="alice",
        )

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph(), tenant_id="demo"
        )

        assert stats.term_count == 2
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def test_pending_count_covers_all_three_review_queues():
    """待审数要把关系审核、疑似重复、属性冲突三个都算上，跟侧边栏一个口径。

    只数其中一个的话，一个「0 条待审关系 + 12 条疑似重复 + 5 条属性冲突」
    的领域在看板上显示「待审 0」、连入口都不给，而侧边栏同时显示
    「数据审核：17」——同一个人同一屏看到两个互相矛盾的数字。

    三个数各不相同（0/12/5）：相同的话，把其中一个接到另一格上的实现也能
    变绿。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_duplicate_suggestions(review_conn, "demo", 12)
        for i in range(5):
            await record_conflict(
                review_conn, tenant_id="demo", node_key=f"产品:P{i}", field="price",
                kept_value="39", kept_source="a.xlsx",
                incoming_value="45", incoming_source="b.xlsx",
            )

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph(), tenant_id="demo"
        )

        assert stats.pending_review_count == 17
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def test_a_graph_failure_surfaces_instead_of_reporting_zero_edges():
    """图谱查不通时抛出去，不是报 0 条关系。

    报 0 的话用户看到的是「这个领域一条关系都没有」——一个看起来正常、
    实际是错的数字。他会据此以为数据没导进去，然后去重跑一遍 ETL。
    调用方接住它，把这张卡渲染成「统计失败」，看得见也够得着纠正。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_terms(review_conn, "demo", 2)

        with pytest.raises(RuntimeError):
            await collect_tenant_stats(
                review_conn, ingestion_conn, FakeGraph(broken={"demo"}), tenant_id="demo"
            )
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def _seed_source_terms(conn: aiosqlite.Connection, tenant_id: str, source: str, count: int) -> None:
    for i in range(count):
        await create_term(
            conn, tenant_id=tenant_id, standard_name=f"{source}-{i}", aliases=[],
            term_type="产品", source=source,
        )


async def test_sheet_row_count_only_counts_etl_rows():
    """「表格行数」只数表格/数据库导入进来的实体（source='etl'）。

    手工录入（manual）和审核创建（review）的不算。三种来源各造不同数量：
    数全部 terms 的实现会得到 6 而不是 3。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_source_terms(review_conn, "demo", "etl", 3)
        await _seed_source_terms(review_conn, "demo", "manual", 2)
        await _seed_source_terms(review_conn, "demo", "review", 1)

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph(), tenant_id="demo"
        )

        assert stats.sheet_row_count == 3
        assert stats.term_count == 6
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def test_sheet_row_count_excludes_manually_deleted_rows():
    """人工删除（__deleted__）的 etl 行不算。

    看板上的数字要等于用户在实体明细页按来源筛出来的条数。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_source_terms(review_conn, "demo", "etl", 3)
        await upsert_term_edit(
            review_conn, tenant_id="demo", node_key="产品:etl-0",
            field=FIELD_DELETED, value="1", edited_by="alice",
        )

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph(), tenant_id="demo"
        )

        assert stats.sheet_row_count == 2
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def test_stale_question_count_matches_the_persona_endpoint():
    """配三条手写问题，其中一条提到的东西本体里没有 → 计数是 1。

    口径必须跟 GET /{tenant_id}/persona/stale-questions 完全一致（同一个
    函数）：看板说 1 条失效、数字人页列出 2 条，用户会以为其中一处坏了。
    """
    from app.graphrag.question_validation import find_unmatched_questions
    from app.graphrag.tenant_personas_store import (
        ensure_tenant_personas_schema,
        get_questions,
        set_questions,
    )
    from app.graphrag.terms_store import list_terms_merged

    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await ensure_tenant_personas_schema(review_conn)
        await create_term(
            review_conn, tenant_id="demo", standard_name="Beer", aliases=[], term_type="产品"
        )
        await set_questions(
            review_conn, tenant_id="demo",
            questions=["Beer 是哪个产地的？", "产品有哪些口味？", "库存还有多少？"],
        )

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph(), tenant_id="demo"
        )

        assert stats.stale_question_count == 1
        # 跟端点用的那份口径逐字相同。
        expected = find_unmatched_questions(
            await get_questions(review_conn, "demo"), await list_terms_merged(review_conn, "demo")
        )
        assert stats.stale_question_count == len(expected) == 1
    finally:
        await review_conn.close()
        await ingestion_conn.close()


async def test_a_tenant_without_a_persona_row_has_zero_stale_questions():
    """没配过数字人的租户：0，不抛异常。

    tenant_personas 里没这一行是常态（存量租户都没配），抛异常的话整张卡
    变成「统计失败」，而它明明什么都没坏。
    """
    from app.graphrag.tenant_personas_store import ensure_tenant_personas_schema

    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await ensure_tenant_personas_schema(review_conn)

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph(), tenant_id="demo"
        )

        assert stats.stale_question_count == 0
    finally:
        await review_conn.close()
        await ingestion_conn.close()
