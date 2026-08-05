import aiosqlite

from app.ingestion.ingestion_queue import (
    enqueue_ingestion_job,
    ensure_ingestion_queue_schema,
    list_pending_jobs,
    mark_job_completed,
    mark_job_failed,
    process_pending_jobs,
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
