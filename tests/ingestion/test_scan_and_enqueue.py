import aiosqlite

from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema, list_pending_jobs
from app.ingestion.scan_and_enqueue import scan_and_enqueue
from app.ingestion.tracking import ensure_tracking_schema, record_ingested


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)
    return conn


async def test_enqueues_ingest_jobs_for_new_files(tmp_path):
    (tmp_path / "a.md").write_text("内容A", encoding="utf-8")
    conn = await _connect()

    summary = await scan_and_enqueue(tmp_path, tenant_id="t1", conn=conn)

    assert summary == {"new": 1, "changed": 0, "deleted": 0, "unchanged": 0}
    pending = await list_pending_jobs(conn)
    assert len(pending) == 1
    assert pending[0]["action"] == "ingest"


async def test_enqueues_delete_jobs_for_removed_files(tmp_path):
    conn = await _connect()
    missing = tmp_path / "removed.md"
    await record_ingested(
        conn, tenant_id="t1", file_path=str(missing), content_hash="h1", chunk_count=1
    )

    summary = await scan_and_enqueue(tmp_path, tenant_id="t1", conn=conn)

    assert summary["deleted"] == 1
    pending = await list_pending_jobs(conn)
    assert len(pending) == 1
    assert pending[0]["action"] == "delete"


async def test_does_not_enqueue_anything_for_unchanged_files(tmp_path):
    file_path = tmp_path / "a.md"
    file_path.write_text("内容A", encoding="utf-8")
    conn = await _connect()

    from app.ingestion.tracking import compute_file_hash

    await record_ingested(
        conn,
        tenant_id="t1",
        file_path=str(file_path),
        content_hash=compute_file_hash(file_path),
        chunk_count=1,
    )

    summary = await scan_and_enqueue(tmp_path, tenant_id="t1", conn=conn)

    assert summary == {"new": 0, "changed": 0, "deleted": 0, "unchanged": 1}
    assert await list_pending_jobs(conn) == []
