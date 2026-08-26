import aiosqlite
import pytest

from app.graphrag.duplicate_detection_worker import main
from app.graphrag.duplicate_review_queue import (
    ensure_duplicate_review_schema,
    list_pending_duplicate_suggestions,
    reject_duplicate_suggestion,
)
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema
from app.graphrag.terms_store import create_term, ensure_terms_schema


@pytest.fixture
async def conn(tmp_path):
    async with aiosqlite.connect(":memory:") as conn:
        await ensure_duplicate_review_schema(conn)
        await ensure_terms_schema(conn, seed_yaml_path=tmp_path / "empty.yaml")
        # create_term validates term_type against the confirmed ontology
        # categories for the tenant (app/graphrag/terms_store.py::
        # _validate_categories) -- the brief's fixture omitted this, so
        # create_term("t1", term_type="公司"/"产品") would otherwise raise
        # UnknownCategoryError. Seed both term types used below for tenant t1.
        await ensure_ontology_schema(conn)
        await create_term_type(conn, tenant_id="t1", value="公司")
        await create_term_type(conn, tenant_id="t1", value="产品")
        await confirm_ontology(conn, "t1")
        yield conn


async def test_main_enqueues_suggestion_for_similar_terms(conn):
    # "可口可乐"（term B 的 standard_name）是 term A 的 alias "可口可乐" 的
    # 精确超串，两者相似度算 1.0（跟 test_duplicate_detection.py 里
    # _COCA/_KEKOULE 的关系一致），但不是完全同名——terms_store.create_term
    # 的同租户同类型唯一性约束只禁止"标准名/别名完全相同"，这里 "可口可乐"
    # 和 "可口可乐股份" 不相等，两条术语都能正常创建。
    await create_term(
        conn, tenant_id="t1", standard_name="Coca-Cola", aliases=["可口可乐"],
        term_type="公司", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐股份", aliases=[],
        term_type="公司", source="manual",
    )

    processed = await main(review_conn=conn, tenant_id="t1")

    assert processed == 1
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    assert len(pending) == 1


async def test_main_does_not_reenqueue_rejected_pair(conn):
    await create_term(
        conn, tenant_id="t1", standard_name="Coca-Cola", aliases=["可口可乐"],
        term_type="公司", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐股份", aliases=[],
        term_type="公司", source="manual",
    )
    await main(review_conn=conn, tenant_id="t1")
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    await reject_duplicate_suggestion(conn, review_id=pending[0]["review_id"], tenant_id="t1")

    processed = await main(review_conn=conn, tenant_id="t1")

    assert processed == 0
    assert await list_pending_duplicate_suggestions(conn, tenant_id="t1") == []


async def test_main_no_similar_terms_enqueues_nothing(conn):
    await create_term(
        conn, tenant_id="t1", standard_name="Coca-Cola", aliases=[],
        term_type="公司", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="Pepsi", aliases=[],
        term_type="公司", source="manual",
    )

    processed = await main(review_conn=conn, tenant_id="t1")

    assert processed == 0


async def test_main_does_not_compare_across_term_types(conn):
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐", aliases=[],
        term_type="公司", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐", aliases=[],
        term_type="产品", source="manual",
    )

    processed = await main(review_conn=conn, tenant_id="t1")

    # 跨类型重名是合法的（见 2026-08-22 那份计划），不应该被当成疑似重复
    assert processed == 0
