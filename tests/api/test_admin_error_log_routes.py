"""报错明细：三个来源各自的失败清单（spec D7）。

这一页要回答的是「有什么坏了、我该做什么」。三个来源的修复动作完全不同——
文档失败去**重试**，表格跳行去**改表格**，问答未命中去**建模**——所以它们
必须分开列，压成一个"错误列表"等于让用户自己猜该干什么。
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.auth.admin_users_store import create_admin_user
from app.graphrag.etl_skipped_rows import (
    SkippedRowRecord,
    ensure_etl_skipped_rows_schema,
    record_skipped_rows,
)
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema
from app.main import app
from app.memory.qa_diagnostics import record_diagnostic
from app.memory.schema import ensure_schema
from tests.schema_fixtures import ensure_admin_auth_schema
from tests.settings_factory import build_settings


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


class _Conns:
    def __init__(self, review, ingestion, memory):
        self.review = review
        self.ingestion = ingestion
        self.memory = memory


@pytest.fixture
def conns():
    async def _open():
        review = await aiosqlite.connect(":memory:")
        ingestion = await aiosqlite.connect(":memory:")
        memory = await aiosqlite.connect(":memory:")
        try:
            await create_tenants_table(review)
            await ensure_admin_auth_schema(review)
            await ensure_etl_skipped_rows_schema(review)
            await ensure_ingestion_queue_schema(ingestion)
            await ensure_schema(memory)
            for tenant in ("demo", "other"):
                await create_tenant(review, tenant_id=tenant, name=tenant)
            await create_admin_user(
                review, username="admin", password="password1", role="admin", tenant_id=None
            )
            await create_admin_user(
                review, username="member", password="password1", role="member",
                tenant_id="demo",
            )
        except BaseException:
            for conn in (review, ingestion, memory):
                await conn.close()
            raise
        return _Conns(review, ingestion, memory)

    opened = asyncio.run(_open())
    try:
        yield opened
    finally:
        for conn in (opened.review, opened.ingestion, opened.memory):
            asyncio.run(conn.close())


async def _seed_job(conn, *, job_id, tenant_id="demo", status="dead", last_error="OCR 超时"):
    await conn.execute(
        "INSERT INTO ingestion_jobs (job_id, dedupe_key, tenant_id, file_path, action,"
        " status, last_error) VALUES (?, ?, ?, ?, 'ingest', ?, ?)",
        (job_id, f"{tenant_id}:{job_id}", tenant_id, f"/uploads/{job_id}.pdf", status, last_error),
    )
    await conn.commit()


async def _seed_qa(conn, *, tenant_id="demo", question="库存多少", outcome="no_match"):
    await record_diagnostic(
        conn, tenant_id=tenant_id, session_id="s1", question=question,
        resolved_question=None, answer="没找到", used_sources=[], tool_results=[],
        outcome=outcome,
    )


def _get(conns, path, *, username="admin", role="admin", tenant_id=None):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_review_conn] = lambda: conns.review
    app.dependency_overrides[deps.get_ingestion_conn] = lambda: conns.ingestion
    app.dependency_overrides[deps.get_memory_conn] = lambda: conns.memory
    try:
        token = session_store.create_session(
            username=username, role=role, tenant_id=tenant_id
        )
        client = TestClient(app)
        return client.get(path, headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()


def test_each_source_returns_only_its_own_failures(conns):
    """三个来源各造两条，逐个端点断言它拿到的正是自己那两条。

    混在一起的话用户没法知道该做什么：重试、改表格、建模是三件事。
    """
    asyncio.run(_seed_job(conns.ingestion, job_id="j1"))
    asyncio.run(_seed_job(conns.ingestion, job_id="j2"))
    asyncio.run(
        record_skipped_rows(
            conns.review, tenant_id="demo", run_id="run-1",
            rows=[
                SkippedRowRecord(
                    label="产品", source_file="商品表.csv", row_number=88, reason="价格非数字"
                ),
                SkippedRowRecord(
                    label="产品", source_file="商品表.csv", row_number=104, reason="缺少名称"
                ),
            ],
        )
    )
    asyncio.run(_seed_qa(conns.memory, question="库存多少"))
    asyncio.run(_seed_qa(conns.memory, question="退货率"))

    documents = _get(conns, "/api/admin/demo/errors/documents").json()
    etl_rows = _get(conns, "/api/admin/demo/errors/etl-rows").json()
    qa = _get(conns, "/api/admin/demo/errors/qa").json()

    assert {d["job_id"] for d in documents["items"]} == {"j1", "j2"}
    assert {r["row_number"] for r in etl_rows["items"]} == {88, 104}
    assert {q["question"] for q in qa["items"]} == {"库存多少", "退货率"}


def test_successful_documents_do_not_appear(conns):
    """已经成功的不该出现。

    批次里同时有成功和失败的——全是失败的话，「返回全部」的实现也能变绿。
    """
    asyncio.run(_seed_job(conns.ingestion, job_id="bad"))
    asyncio.run(
        _seed_job(conns.ingestion, job_id="good", status="completed", last_error=None)
    )

    body = _get(conns, "/api/admin/demo/errors/documents").json()

    assert [d["job_id"] for d in body["items"]] == ["bad"]


def test_answered_questions_do_not_appear_in_the_qa_page(conns):
    """答出来了的不出现。这一页是「答不出来的那些」。

    同样要两种都造：全是失败的话，不过滤的实现也能变绿。
    """
    asyncio.run(_seed_qa(conns.memory, question="没命中的", outcome="no_match"))
    asyncio.run(_seed_qa(conns.memory, question="报错的", outcome="error"))
    asyncio.run(_seed_qa(conns.memory, question="正常答出来的", outcome="answered"))

    body = _get(conns, "/api/admin/demo/errors/qa").json()

    assert {q["question"] for q in body["items"]} == {"没命中的", "报错的"}
    # outcome 要带出来：未命中去建模，报错去看日志，页面要分得开。
    assert {q["outcome"] for q in body["items"]} == {"no_match", "error"}


def test_counts_endpoint_returns_all_three_keys_including_zeroes(conns):
    """空的那一类是 0，不是 key 不存在。

    key 不存在的话前端读到 undefined，角标要么不显示要么显示 NaN——
    而"这一类没有问题"是一个用户需要看到的结论。
    """
    asyncio.run(_seed_job(conns.ingestion, job_id="j1"))

    body = _get(conns, "/api/admin/demo/errors/counts").json()

    assert body == {"documents": 1, "etl_rows": 0, "qa": 0}


def test_the_document_error_carries_the_message_not_just_a_flag(conns):
    """last_error 的内容要带出来。

    只说「失败了」的话，用户不知道是 OCR 超时还是文件格式不支持——两者
    的处理完全不同（重试 vs 换个文件）。
    """
    asyncio.run(_seed_job(conns.ingestion, job_id="j1", last_error="OCR 超时（120s）"))

    body = _get(conns, "/api/admin/demo/errors/documents").json()

    assert body["items"][0]["last_error"] == "OCR 超时（120s）"
    assert body["items"][0]["file_path"].endswith("j1.pdf")


@pytest.mark.parametrize("suffix", ["documents", "etl-rows", "etl-rows.csv", "qa", "counts"])
def test_every_page_is_tenant_scoped(conns, suffix: str):
    """member 拿不到别的租户的报错明细。

    漏挂的那一组在生产上不会有任何报错，请求照常 200，只是返回别人的数据。
    """
    response = _get(
        conns, f"/api/admin/other/errors/{suffix}",
        username="member", role="member", tenant_id="demo",
    )

    assert response.status_code == 403


@pytest.mark.parametrize("suffix", ["documents", "etl-rows", "etl-rows.csv", "qa", "counts"])
def test_admin_is_not_blocked_on_any_page(conns, suffix: str):
    """反面：403 必须来自权限判断，不是因为这条路由整个坏了。

    没有这一条，把守卫写成"一律 403"也能让上面那组全绿。
    """
    assert _get(conns, f"/api/admin/other/errors/{suffix}").status_code == 200


def test_each_page_is_scoped_to_its_tenant(conns):
    """别的租户的失败不出现在这个租户的清单里。"""
    asyncio.run(_seed_job(conns.ingestion, job_id="mine"))
    asyncio.run(_seed_job(conns.ingestion, job_id="theirs", tenant_id="other"))
    asyncio.run(_seed_qa(conns.memory, question="我的"))
    asyncio.run(_seed_qa(conns.memory, tenant_id="other", question="别人的"))
    asyncio.run(
        record_skipped_rows(
            conns.review, tenant_id="other", run_id="run-x",
            rows=[SkippedRowRecord(label="X", source_file="x.csv", row_number=1, reason="r")],
        )
    )

    documents = _get(conns, "/api/admin/demo/errors/documents").json()
    qa = _get(conns, "/api/admin/demo/errors/qa").json()

    assert [d["job_id"] for d in documents["items"]] == ["mine"]
    assert [q["question"] for q in qa["items"]] == ["我的"]
    assert _get(conns, "/api/admin/demo/errors/etl-rows").json()["items"] == []
    assert _get(conns, "/api/admin/demo/errors/counts").json() == {
        "documents": 1, "etl_rows": 0, "qa": 1
    }


def test_the_document_count_is_not_capped_by_the_page_size(conns):
    """角标数的是**全部**失败文档，不是被 limit 截断的那一页。

    拿列表长度充数的话，攒到 250 条时角标恒等于上限（200）——用户以为自己
    看清了问题的规模，实际少报 50 个，而那个数字永远不会再变大。
    """
    for i in range(250):
        asyncio.run(_seed_job(conns.ingestion, job_id=f"j{i}"))

    body = _get(conns, "/api/admin/demo/errors/counts").json()

    assert body["documents"] == 250


def test_each_list_reports_the_real_total_so_the_page_can_say_it_truncated(conns):
    """列表回包带**真实总数**。

    少了它，前端说不出「只列了 50 / 共 250」——而用户眼里的世界就只有那
    50 条，剩下的既不在任何页签里，也没有任何信号让人怀疑它们存在。
    """
    for i in range(60):
        asyncio.run(_seed_job(conns.ingestion, job_id=f"j{i}"))

    body = _get(conns, "/api/admin/demo/errors/documents?limit=10").json()

    assert len(body["items"]) == 10
    assert body["total"] == 60


def test_paging_walks_the_document_list_without_repeating(conns):
    """offset 能翻页，不重不漏。"""
    for i in range(5):
        asyncio.run(_seed_job(conns.ingestion, job_id=f"j{i}"))

    first = _get(conns, "/api/admin/demo/errors/documents?limit=2&offset=0").json()["items"]
    second = _get(conns, "/api/admin/demo/errors/documents?limit=2&offset=2").json()["items"]
    third = _get(conns, "/api/admin/demo/errors/documents?limit=2&offset=4").json()["items"]

    seen = [d["job_id"] for d in first + second + third]
    assert sorted(seen) == sorted(f"j{i}" for i in range(5))


def test_the_csv_download_carries_every_skipped_row(conns):
    """「下载这批」下的是**整份**，不是当前这一页。

    用户要改的是全部——只给一页的话，他改完再导入还会跳一批，而他以为
    已经改完了。
    """
    asyncio.run(
        record_skipped_rows(
            conns.review, tenant_id="demo", run_id="run-1",
            rows=[
                SkippedRowRecord(
                    label="产品", source_file="商品表.csv", row_number=i, reason="价格非数字"
                )
                for i in range(120)
            ],
        )
    )

    response = _get(conns, "/api/admin/demo/errors/etl-rows.csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    lines = [line for line in response.text.splitlines() if line]
    assert len(lines) == 121, "表头 + 120 行"
    assert "价格非数字" in response.text


def test_the_csv_only_contains_this_tenants_rows(conns):
    """别的租户的跳过行不能下到这份 CSV 里。"""
    asyncio.run(
        record_skipped_rows(
            conns.review, tenant_id="other", run_id="run-x",
            rows=[SkippedRowRecord(label="X", source_file="别人的.csv", row_number=1, reason="r")],
        )
    )

    response = _get(conns, "/api/admin/demo/errors/etl-rows.csv")

    assert "别人的.csv" not in response.text
