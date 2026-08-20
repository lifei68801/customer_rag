import asyncio

import aiosqlite
import pytest

from app.ingestion.ingestion_queue import (
    JobNotDeadError,
    JobNotFoundError,
    delete_job,
    enqueue_ingestion_job,
    ensure_ingestion_queue_schema,
    list_dead_jobs,
    list_pending_jobs,
    mark_job_completed,
    mark_job_failed,
    mark_job_started,
    process_pending_jobs,
    retry_job,
)
from app.ingestion.tracking import (
    compute_file_hash,
    ensure_tracking_schema,
    get_tracked_hash,
)
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.retrieval.vector_store import InMemoryVectorStore


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)
    return conn


async def _backdate_updated_at(conn: aiosqlite.Connection, job_id: str, *, minutes_ago: int) -> None:
    """把某条任务的 updated_at（_is_stuck_pending 的判据）直接改写到 N 分钟
    前，绕开真实时间流逝来测试阈值判断（_STUCK_AFTER_MINUTES = 30）。"""
    await conn.execute(
        "UPDATE ingestion_jobs SET updated_at = datetime('now', ?) WHERE job_id = ?",
        (f"-{minutes_ago} minutes", job_id),
    )
    await conn.commit()


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


async def test_enqueue_creates_a_pending_job():
    conn = await _connect()

    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    pending = await list_pending_jobs(conn)
    assert len(pending) == 1
    assert pending[0]["job_id"] == job_id
    assert pending[0]["action"] == "ingest"


async def test_enqueue_is_idempotent_for_the_same_file_and_hash():
    conn = await _connect()

    job_id_1 = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    job_id_2 = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    assert job_id_1 == job_id_2
    assert len(await list_pending_jobs(conn)) == 1


async def test_list_pending_jobs_filters_by_tenant_id_in_sql():
    conn = await _connect()
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await enqueue_ingestion_job(
        conn, tenant_id="t2", file_path="b.md", content_hash="h2", action="ingest"
    )

    t1_jobs = await list_pending_jobs(conn, tenant_id="t1")

    assert len(t1_jobs) == 1
    assert t1_jobs[0]["tenant_id"] == "t1"


async def test_list_pending_jobs_without_tenant_id_returns_all_tenants():
    conn = await _connect()
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await enqueue_ingestion_job(
        conn, tenant_id="t2", file_path="b.md", content_hash="h2", action="ingest"
    )

    assert len(await list_pending_jobs(conn)) == 2


async def test_mark_job_completed_removes_it_from_pending():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    await mark_job_completed(conn, job_id)

    assert await list_pending_jobs(conn) == []


async def test_mark_job_failed_retries_until_max_attempts_then_goes_dead():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    await mark_job_failed(conn, job_id, error="失败1", max_attempts=2)
    assert len(await list_pending_jobs(conn)) == 1

    await mark_job_failed(conn, job_id, error="失败2", max_attempts=2)
    assert await list_pending_jobs(conn) == []


async def test_process_pending_jobs_ingests_a_new_markdown_file_and_records_tracking(
    tmp_path,
):
    file_path = tmp_path / "a.md"
    file_path.write_text("## 主题\n内容。\n", encoding="utf-8")
    conn = await _connect()
    content_hash = compute_file_hash(file_path)
    await enqueue_ingestion_job(
        conn,
        tenant_id="t1",
        file_path=str(file_path),
        content_hash=content_hash,
        action="ingest",
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    processed = await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    assert processed == 1
    assert await list_pending_jobs(conn) == []
    tracked_hash = await get_tracked_hash(
        conn, tenant_id="t1", file_path=str(file_path)
    )
    assert tracked_hash == content_hash

    results = await vector_store.search(
        query_vector=[0.1, 0.2], top_k=10, tenant_id="t1"
    )
    assert any("内容" in r.text for r in results)


async def test_process_pending_jobs_deletes_old_chunks_before_reingesting_changed_file(
    tmp_path,
):
    file_path = tmp_path / "a.md"
    file_path.write_text("## 新主题\n新内容。\n", encoding="utf-8")
    conn = await _connect()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    from app.retrieval.vector_store import VectorRecord

    await vector_store.upsert(
        [
            VectorRecord(
                id=f"{file_path}#0",
                vector=[0.1, 0.2],
                text="旧内容，应该被删除",
                tenant_id="t1",
                metadata={"source": str(file_path)},
            )
        ]
    )

    content_hash = compute_file_hash(file_path)
    await enqueue_ingestion_job(
        conn,
        tenant_id="t1",
        file_path=str(file_path),
        content_hash=content_hash,
        action="ingest",
    )

    await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    results = await vector_store.search(
        query_vector=[0.1, 0.2], top_k=10, tenant_id="t1"
    )
    texts = {r.text for r in results}
    assert "旧内容，应该被删除" not in texts
    assert any("新内容" in t for t in texts)


async def test_process_pending_jobs_handles_delete_action(tmp_path):
    file_path = tmp_path / "removed.md"
    conn = await _connect()

    await record_ingested_for_test(conn, str(file_path))

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    from app.retrieval.vector_store import VectorRecord

    await vector_store.upsert(
        [
            VectorRecord(
                id=f"{file_path}#0",
                vector=[0.1, 0.2],
                text="即将被删除的文档",
                tenant_id="t1",
                metadata={"source": str(file_path)},
            )
        ]
    )

    await enqueue_ingestion_job(
        conn,
        tenant_id="t1",
        file_path=str(file_path),
        content_hash="",
        action="delete",
    )

    processed = await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    assert processed == 1
    results = await vector_store.search(
        query_vector=[0.1, 0.2], top_k=10, tenant_id="t1"
    )
    assert results == []
    assert await get_tracked_hash(conn, tenant_id="t1", file_path=str(file_path)) is None


async def record_ingested_for_test(conn, file_path: str) -> None:
    from app.ingestion.tracking import record_ingested

    await record_ingested(
        conn, tenant_id="t1", file_path=file_path, content_hash="old-hash", chunk_count=1
    )


async def test_process_pending_jobs_marks_failed_job_for_retry_without_crashing_batch(
    tmp_path,
):
    missing_file = tmp_path / "does_not_exist.md"
    conn = await _connect()
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(missing_file), content_hash="h1",
        action="ingest",
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    processed = await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    assert processed == 0
    pending = await list_pending_jobs(conn)
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1


async def test_process_pending_jobs_skips_graph_extraction_when_build_graph_is_false(
    tmp_path,
):
    file_path = tmp_path / "a.md"
    file_path.write_text("## 主题\n内容。\n", encoding="utf-8")
    conn = await _connect()
    content_hash = compute_file_hash(file_path)
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(file_path), content_hash=content_hash,
        action="ingest", build_graph=False,
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    graph_calls = {"count": 0}

    class FailingGraphClient:
        async def merge_relation(self, **kwargs):
            graph_calls["count"] += 1

    await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        graph_client=FailingGraphClient(),
        graph_llm_registry="not-none-marker",
        graph_llm_provider_name="fake-embedding",
        graph_terms=[],
    )

    # build_graph=False 时即使外部传了图谱资源，这条任务也不该触发图谱抽取
    assert graph_calls["count"] == 0


async def test_enqueue_records_build_graph_flag():
    conn = await _connect()

    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest",
        build_graph=True,
    )

    pending = await list_pending_jobs(conn)
    assert pending[0]["job_id"] == job_id
    assert pending[0]["build_graph"] == 1


async def test_enqueue_different_build_graph_flag_creates_separate_job():
    conn = await _connect()

    job_id_1 = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest",
        build_graph=False,
    )
    job_id_2 = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest",
        build_graph=True,
    )

    assert job_id_1 != job_id_2
    assert len(await list_pending_jobs(conn)) == 2


async def test_process_pending_jobs_processes_documents_concurrently_when_job_concurrency_above_one(tmp_path):
    """job_concurrency=2 时，两份 markdown 文档的摄取应该并发跑，不是排队
    顺序执行——用两次 embedding 调用互等的方式证明：如果退化回顺序执行，
    第一份文档的 embedding 调用会一直等不到第二份文档的 embedding 调用
    启动，卡到 asyncio.wait_for 超时。
    """
    conn = await _connect()
    (tmp_path / "a.md").write_text("## 主题A\n内容A。\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("## 主题B\n内容B。\n", encoding="utf-8")
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(tmp_path / "a.md"),
        content_hash="hash-a", action="ingest",
    )
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(tmp_path / "b.md"),
        content_hash="hash-b", action="ingest",
    )

    started_count = {"n": 0}
    both_started = asyncio.Event()

    class SyncEmbeddingProvider:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
            started_count["n"] += 1
            if started_count["n"] >= 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", SyncEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    processed = await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        job_concurrency=2,
    )

    assert processed == 2


async def test_list_dead_jobs_returns_only_dead_status_scoped_to_tenant():
    conn = await _connect()
    dead_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, dead_id, error="解析失败", max_attempts=1)
    pending_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="b.md", content_hash="h2", action="ingest"
    )
    other_tenant_dead_id = await enqueue_ingestion_job(
        conn, tenant_id="t2", file_path="c.md", content_hash="h3", action="ingest"
    )
    await mark_job_failed(conn, other_tenant_dead_id, error="解析失败", max_attempts=1)

    dead_jobs = await list_dead_jobs(conn, tenant_id="t1")

    assert [j["job_id"] for j in dead_jobs] == [dead_id]
    assert pending_id not in [j["job_id"] for j in dead_jobs]


async def test_retry_job_resets_dead_job_to_pending_with_attempts_cleared():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, job_id, error="解析失败", max_attempts=1)
    assert await list_dead_jobs(conn, tenant_id="t1") != []

    await retry_job(conn, job_id, tenant_id="t1")

    dead_after = await list_dead_jobs(conn, tenant_id="t1")
    assert dead_after == []
    pending_after = await list_pending_jobs(conn, tenant_id="t1")
    assert len(pending_after) == 1
    assert pending_after[0]["job_id"] == job_id
    assert pending_after[0]["attempts"] == 0
    assert pending_after[0]["last_error"] is None


async def test_retry_job_raises_when_job_belongs_to_another_tenant():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, job_id, error="解析失败", max_attempts=1)

    with pytest.raises(JobNotFoundError):
        await retry_job(conn, job_id, tenant_id="t2")


async def test_retry_job_raises_when_job_is_not_dead():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    with pytest.raises(JobNotDeadError):
        await retry_job(conn, job_id, tenant_id="t1")


async def test_delete_job_removes_dead_job_and_returns_its_file_path():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_failed(conn, job_id, error="解析失败", max_attempts=1)

    file_path = await delete_job(conn, job_id, tenant_id="t1")

    assert file_path == "a.md"
    assert await list_dead_jobs(conn, tenant_id="t1") == []


async def test_delete_job_raises_when_job_is_not_dead():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    with pytest.raises(JobNotDeadError):
        await delete_job(conn, job_id, tenant_id="t1")
    # 还在 pending，没有被误删
    assert len(await list_pending_jobs(conn, tenant_id="t1")) == 1


async def test_list_pending_jobs_does_not_mark_freshly_enqueued_job_as_stuck():
    conn = await _connect()
    await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    pending = await list_pending_jobs(conn, tenant_id="t1")

    assert pending[0]["started_at"] is None
    assert pending[0]["is_stuck"] is False


async def test_list_pending_jobs_marks_never_started_job_as_stuck_after_threshold():
    # 回归测试：本系统没有周期性调度器，一条从未被 worker 取用过
    # （started_at 为空）的 pending 任务如果长期没有其他上传/重试触发
    # 批处理，会永远卡在队列里——这种情况也必须能被判定为疑似卡死，
    # 不能因为 started_at 是空的就放过。
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await _backdate_updated_at(conn, job_id, minutes_ago=31)

    pending = await list_pending_jobs(conn, tenant_id="t1")

    assert pending[0]["started_at"] is None
    assert pending[0]["is_stuck"] is True


async def test_list_pending_jobs_does_not_mark_recently_started_job_as_stuck():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_started(conn, job_id)

    pending = await list_pending_jobs(conn, tenant_id="t1")

    assert pending[0]["is_stuck"] is False


async def test_list_pending_jobs_marks_job_as_stuck_after_threshold():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_started(conn, job_id)
    await _backdate_updated_at(conn, job_id, minutes_ago=31)

    pending = await list_pending_jobs(conn, tenant_id="t1")

    assert pending[0]["is_stuck"] is True


async def test_list_pending_jobs_does_not_mark_job_stuck_just_under_threshold():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_started(conn, job_id)
    await _backdate_updated_at(conn, job_id, minutes_ago=29)

    pending = await list_pending_jobs(conn, tenant_id="t1")

    assert pending[0]["is_stuck"] is False


async def test_retry_job_raises_for_pending_job_that_is_not_stuck():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_started(conn, job_id)
    await _backdate_updated_at(conn, job_id, minutes_ago=5)

    with pytest.raises(JobNotDeadError):
        await retry_job(conn, job_id, tenant_id="t1")


async def test_retry_job_clears_started_at_for_stuck_pending_job():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_started(conn, job_id)
    await _backdate_updated_at(conn, job_id, minutes_ago=45)

    await retry_job(conn, job_id, tenant_id="t1")

    pending_after = await list_pending_jobs(conn, tenant_id="t1")
    assert len(pending_after) == 1
    assert pending_after[0]["job_id"] == job_id
    assert pending_after[0]["status"] == "pending"
    assert pending_after[0]["started_at"] is None
    assert pending_after[0]["is_stuck"] is False


async def test_retry_job_recovers_a_never_started_stuck_job():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await _backdate_updated_at(conn, job_id, minutes_ago=45)

    await retry_job(conn, job_id, tenant_id="t1")

    pending_after = await list_pending_jobs(conn, tenant_id="t1")
    assert len(pending_after) == 1
    assert pending_after[0]["status"] == "pending"
    assert pending_after[0]["is_stuck"] is False


async def test_delete_job_raises_for_pending_job_that_is_not_stuck():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_started(conn, job_id)
    await _backdate_updated_at(conn, job_id, minutes_ago=5)

    with pytest.raises(JobNotDeadError):
        await delete_job(conn, job_id, tenant_id="t1")
    assert len(await list_pending_jobs(conn, tenant_id="t1")) == 1


async def test_delete_job_removes_stuck_pending_job_and_returns_its_file_path():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )
    await mark_job_started(conn, job_id)
    await _backdate_updated_at(conn, job_id, minutes_ago=45)

    file_path = await delete_job(conn, job_id, tenant_id="t1")

    assert file_path == "a.md"
    assert await list_pending_jobs(conn, tenant_id="t1") == []


async def test_mark_job_started_sets_started_at():
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", action="ingest"
    )

    await mark_job_started(conn, job_id)

    pending = await list_pending_jobs(conn, tenant_id="t1")
    assert pending[0]["started_at"] is not None


async def test_process_pending_jobs_sets_started_at_before_processing_even_on_failure(
    tmp_path,
):
    missing_file = tmp_path / "does_not_exist.md"
    conn = await _connect()
    job_id = await enqueue_ingestion_job(
        conn, tenant_id="t1", file_path=str(missing_file), content_hash="h1",
        action="ingest",
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    await process_pending_jobs(
        conn,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    pending = await list_pending_jobs(conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["job_id"] == job_id
    assert pending[0]["started_at"] is not None
