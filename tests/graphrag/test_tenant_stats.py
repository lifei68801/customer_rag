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
from app.graphrag.duplicate_review_queue import (
    ensure_duplicate_review_schema,
    enqueue_duplicate_suggestion,
)
from app.graphrag.review_queue import ensure_review_schema, enqueue_for_review
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


async def test_pending_count_covers_both_review_queues():
    """待审数要把关系审核和疑似重复都算上，跟侧边栏一个口径。

    只数关系队列的话，一个「0 条待审关系 + 12 条疑似重复」的领域在看板上
    显示「待审 0」、连入口都不给，而侧边栏同时显示「数据审核：12」——
    同一个人同一屏看到两个互相矛盾的数字，他会以为其中一个坏了。

    关系队列故意留空：两个都非空的话，「只数了其中一个」的实现会因为数字
    对不上而红，但红的原因说不清是漏了哪一个。
    """
    review_conn = await _review_conn()
    ingestion_conn = await _ingestion_conn()
    try:
        await _seed_duplicate_suggestions(review_conn, "demo", 12)

        stats = await collect_tenant_stats(
            review_conn, ingestion_conn, FakeGraph(), tenant_id="demo"
        )

        assert stats.pending_review_count == 12
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
