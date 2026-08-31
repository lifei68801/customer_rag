import logging

import aiosqlite
import pytest

import app.graphrag.duplicate_detection_worker as duplicate_detection_worker_module
from app.graphrag.duplicate_detection_worker import main
from app.graphrag.duplicate_review_queue import (
    approve_duplicate_suggestion,
    ensure_duplicate_review_schema,
    list_pending_duplicate_suggestions,
    reject_duplicate_suggestion,
)
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.tenants_store import create_tenants_table
from app.graphrag.ontology_lifecycle import confirm_ontology, ensure_ontology_schema
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import create_term, ensure_terms_schema, get_term, list_terms, update_term


@pytest.fixture
async def conn(tmp_path):
    async with aiosqlite.connect(":memory:") as conn:
        await ensure_duplicate_review_schema(conn)
        await ensure_terms_schema(conn, seed_yaml_path=tmp_path / "empty.yaml")
        # Task 3：_scan_tenant() 现在经 list_terms_merged() 读术语表，测试
        # 连接要把 term_edits 表也建好，否则会报 "no such table: term_edits"。
        await ensure_term_edits_schema(conn)
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


async def test_main_warns_when_tenant_registry_is_empty(conn, caplog):
    """租户注册表为空时必须大声告警，不能静默地"扫描 0 个租户"。

    租户注册表的跨库回填现在由 app/main.py 的 lifespan 在启动时做（它要同时
    读本体库和 ingestion 库才能发现历史 tenant_id）。这个 worker 是独立的
    CLI 进程，不走 lifespan——如果它跑在一个 API 进程从没启动过的库文件上，
    注册表就是空的。此时"扫描了 0 个租户、新增 0 条建议"跟"扫描完毕、确实
    没有重复"在输出上完全一样，是一个静默失效的安全网，比没有更糟。
    """
    await create_tenants_table(conn)

    with caplog.at_level(logging.WARNING):
        enqueued = await main(review_conn=conn)

    assert enqueued == 0
    assert any("租户注册表为空" in record.message for record in caplog.records)


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


async def test_main_excludes_tombstoned_terms_from_candidates(conn):
    """Fix 1：已经被合并（approve_duplicate_suggestion 打上"[已合并] "
    墓碑标记）的行不该再被当作候选参与两两比对——墓碑串本身包含被合并前
    的原始名字（node_key 通常带着原 standard_name），短名字很容易跟它
    算出很高的相似度，一旦被再次建议并批准，墓碑串本身会被当垃圾数据
    写进另一条术语的 aliases（见 Fix 1 的调查记录）。"""
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐", aliases=[],
        term_type="公司", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐股份", aliases=[],
        term_type="公司", source="manual",
    )
    # 把第二条术语按 approve_duplicate_suggestion 实际使用的墓碑格式打上
    # 标记（走真实的 update_term，不是直接改数据库行，保证格式跟生产
    # 代码一致）。
    await update_term(
        conn, tenant_id="t1", node_key="公司:可口可乐股份",
        new_standard_name="[已合并] 公司:可口可乐股份", aliases=[],
        term_type="公司",
    )

    processed = await main(review_conn=conn, tenant_id="t1")

    assert processed == 0
    assert await list_pending_duplicate_suggestions(conn, tenant_id="t1") == []


async def test_scan_tenant_skips_bucket_over_pairwise_scan_limit(conn, monkeypatch, caplog):
    """Fix 4：单个 term_type 分组超过临时上限时，整组跳过不比对（不崩溃、
    不静默假装比对过了），并留一条 WARNING 日志点名租户/类型/条数。"""
    monkeypatch.setattr(
        duplicate_detection_worker_module, "_MAX_BUCKET_SIZE_FOR_PAIRWISE_SCAN", 1
    )
    await create_term(
        conn, tenant_id="t1", standard_name="Coca-Cola", aliases=["可口可乐"],
        term_type="公司", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐股份", aliases=[],
        term_type="公司", source="manual",
    )

    with caplog.at_level("WARNING", logger="app.graphrag.duplicate_detection_worker"):
        processed = await main(review_conn=conn, tenant_id="t1")

    assert processed == 0
    assert await list_pending_duplicate_suggestions(conn, tenant_id="t1") == []
    assert any(
        "超过两两比对的临时上限" in record.message for record in caplog.records
    )


async def test_main_second_run_does_not_resuggest_approved_tombstoned_pair(conn):
    """Fix 8（一部分）：approve 之后再跑一次 worker，不应该重新建议这一对
    （既因为 has_any_duplicate_record 已经有 approved 记录，也因为墓碑行
    被 Fix 1 的过滤挡在候选池外——双重保险，任何一个失效都能被这个用例
    抓到）。"""
    await create_term(
        conn, tenant_id="t1", standard_name="Coca-Cola", aliases=["可口可乐"],
        term_type="公司", source="manual",
    )
    await create_term(
        conn, tenant_id="t1", standard_name="可口可乐股份", aliases=[],
        term_type="公司", source="manual",
    )

    first_run_processed = await main(review_conn=conn, tenant_id="t1")
    assert first_run_processed == 1
    pending = await list_pending_duplicate_suggestions(conn, tenant_id="t1")
    assert len(pending) == 1
    await approve_duplicate_suggestion(
        conn, review_id=pending[0]["review_id"], tenant_id="t1",
        keep_node_key="公司:Coca-Cola",
    )

    second_run_processed = await main(review_conn=conn, tenant_id="t1")

    assert second_run_processed == 0
    assert await list_pending_duplicate_suggestions(conn, tenant_id="t1") == []
    keeper = await get_term(conn, "t1", "Coca-Cola", "公司")
    assert set(keeper.aliases) == {"可口可乐", "可口可乐股份"}
    all_terms = await list_terms(conn, "t1")
    merged_row = next(t for t in all_terms if t.node_key == "公司:可口可乐股份")
    assert merged_row.standard_name.startswith("[已合并] ")
    assert merged_row.aliases == []
