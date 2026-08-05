import aiosqlite

from app.ingestion.tracking import ensure_tracking_schema, record_ingested
from app.ingestion.scan_changes import scan_for_changes


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    return conn


async def test_scan_reports_all_files_as_new_when_nothing_tracked_yet(tmp_path):
    (tmp_path / "a.md").write_text("内容A", encoding="utf-8")
    (tmp_path / "b.md").write_text("内容B", encoding="utf-8")
    conn = await _connect()

    result = await scan_for_changes(tmp_path, tenant_id="t1", tracking_conn=conn)

    assert {p.name for p in result.new_files} == {"a.md", "b.md"}
    assert result.changed_files == []
    assert result.unchanged_files == []
    assert result.deleted_file_paths == []


async def test_scan_reports_unchanged_file_when_hash_matches(tmp_path):
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

    result = await scan_for_changes(tmp_path, tenant_id="t1", tracking_conn=conn)

    assert result.new_files == []
    assert result.changed_files == []
    assert [p.name for p in result.unchanged_files] == ["a.md"]


async def test_scan_reports_changed_file_when_hash_differs(tmp_path):
    file_path = tmp_path / "a.md"
    file_path.write_text("旧内容", encoding="utf-8")
    conn = await _connect()
    await record_ingested(
        conn, tenant_id="t1", file_path=str(file_path), content_hash="stale-hash",
        chunk_count=1,
    )

    result = await scan_for_changes(tmp_path, tenant_id="t1", tracking_conn=conn)

    assert [p.name for p in result.changed_files] == ["a.md"]
    assert result.new_files == []
    assert result.unchanged_files == []


async def test_scan_reports_deleted_file_no_longer_present(tmp_path):
    conn = await _connect()
    missing_path = tmp_path / "removed.md"
    await record_ingested(
        conn, tenant_id="t1", file_path=str(missing_path), content_hash="h1",
        chunk_count=1,
    )

    result = await scan_for_changes(tmp_path, tenant_id="t1", tracking_conn=conn)

    assert result.deleted_file_paths == [str(missing_path)]


async def test_scan_only_considers_supported_extensions(tmp_path):
    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("不该被扫描", encoding="utf-8")
    conn = await _connect()

    result = await scan_for_changes(tmp_path, tenant_id="t1", tracking_conn=conn)

    assert {p.name for p in result.new_files} == {"a.md"}
