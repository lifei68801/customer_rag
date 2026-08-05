import aiosqlite

from app.ingestion.tracking import (
    compute_file_hash,
    ensure_tracking_schema,
    get_tracked_hash,
    list_tracked_files,
    record_ingested,
    remove_tracked_file,
)


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    return conn


def test_compute_file_hash_is_stable_for_same_content(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("内容", encoding="utf-8")

    assert compute_file_hash(path) == compute_file_hash(path)


def test_compute_file_hash_differs_for_different_content(tmp_path):
    path_a = tmp_path / "a.md"
    path_a.write_text("内容A", encoding="utf-8")
    path_b = tmp_path / "b.md"
    path_b.write_text("内容B", encoding="utf-8")

    assert compute_file_hash(path_a) != compute_file_hash(path_b)


async def test_get_tracked_hash_returns_none_when_never_ingested():
    conn = await _connect()

    result = await get_tracked_hash(conn, tenant_id="t1", file_path="a.md")

    assert result is None


async def test_record_then_get_returns_the_hash():
    conn = await _connect()

    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="abc123", chunk_count=2
    )

    result = await get_tracked_hash(conn, tenant_id="t1", file_path="a.md")
    assert result == "abc123"


async def test_record_ingested_overwrites_previous_hash_for_same_file():
    conn = await _connect()
    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="old", chunk_count=2
    )

    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="new", chunk_count=3
    )

    result = await get_tracked_hash(conn, tenant_id="t1", file_path="a.md")
    assert result == "new"


async def test_list_tracked_files_scoped_to_tenant():
    conn = await _connect()
    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
    )
    await record_ingested(
        conn, tenant_id="t2", file_path="a.md", content_hash="h2", chunk_count=1
    )

    tracked = await list_tracked_files(conn, tenant_id="t1")

    assert [t["file_path"] for t in tracked] == ["a.md"]


async def test_remove_tracked_file_removes_it_from_list():
    conn = await _connect()
    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=1
    )

    await remove_tracked_file(conn, tenant_id="t1", file_path="a.md")

    assert await list_tracked_files(conn, tenant_id="t1") == []
