import aiosqlite

from app.ingestion.tracking import (
    compute_file_hash,
    count_tracked_files,
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


async def _seed_with_explicit_timestamps(conn: aiosqlite.Connection) -> None:
    """record_ingested() 的时间戳用 datetime('now')（秒级精度），同一个测试
    里连续插入 3 条记录很可能落在同一秒，导致 ORDER BY last_ingested_at DESC
    出现并列、分页测试的期望顺序不稳定。这里直接写显式的、彼此不同的
    last_ingested_at 值，让分页结果的顺序可预测。
    """
    for index, timestamp in enumerate(
        ["2024-01-01T00:00:00", "2024-01-02T00:00:00", "2024-01-03T00:00:00"]
    ):
        await conn.execute(
            "INSERT INTO ingested_documents "
            "(tenant_id, file_path, content_hash, chunk_count, last_ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t1", f"file{index}.md", f"h{index}", 1, timestamp),
        )
    await conn.commit()


async def test_list_tracked_files_paginates_with_limit_and_offset():
    """种 3 条记录（按 last_ingested_at DESC 排序为 file2、file1、file0），
    limit=1 offset=1 应该只拿到第 2 条（file1）。"""
    conn = await _connect()
    await _seed_with_explicit_timestamps(conn)

    page = await list_tracked_files(conn, tenant_id="t1", limit=1, offset=1)

    assert [t["file_path"] for t in page] == ["file1.md"]


async def test_count_tracked_files_returns_total_regardless_of_pagination():
    conn = await _connect()
    await _seed_with_explicit_timestamps(conn)

    total = await count_tracked_files(conn, tenant_id="t1")

    assert total == 3


async def test_list_tracked_files_without_limit_offset_returns_full_unpaginated_list():
    """不传 limit/offset 时必须保持改造前的行为：返回该租户全部追踪记录
    ——这是既有调用方（摄取管线、scan_changes、eval runner 等）赖以不变的
    默认行为。"""
    conn = await _connect()
    await _seed_with_explicit_timestamps(conn)

    tracked = await list_tracked_files(conn, tenant_id="t1")

    assert [t["file_path"] for t in tracked] == ["file2.md", "file1.md", "file0.md"]


# ── 图谱状态 ────────────────────────────────────────────────────────────
#
# 本体未确认时 pipeline 会跳过图谱抽取，文档照样入库。上传前的提示只覆盖
# 「接下来会怎样」；已经传完的那批文档，用户事后无从知道哪些没有图谱，
# 只能全部重传。这一列就是给事后查的。


async def test_records_graph_status():
    conn = await _connect()

    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="h", chunk_count=3,
        graph_status="skipped_ontology_unconfirmed",
    )

    rows = await list_tracked_files(conn, tenant_id="t1")
    assert rows[0]["graph_status"] == "skipped_ontology_unconfirmed"


async def test_graph_status_is_none_when_not_given():
    """老调用方不传这个参数时是 None，而不是猜一个值——「不知道有没有建图」
    和「确定没建图」是两回事，界面上该说的话也不一样。"""
    conn = await _connect()

    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="h", chunk_count=3,
    )

    rows = await list_tracked_files(conn, tenant_id="t1")
    assert rows[0]["graph_status"] is None


async def test_reingest_overwrites_graph_status():
    """确认本体后重传，状态要跟着变好——否则那条警告会一直挂着，
    用户以为自己没修好。"""
    conn = await _connect()
    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="h1", chunk_count=3,
        graph_status="skipped_ontology_unconfirmed",
    )

    await record_ingested(
        conn, tenant_id="t1", file_path="a.md", content_hash="h2", chunk_count=3,
        graph_status="built",
    )

    rows = await list_tracked_files(conn, tenant_id="t1")
    assert rows[0]["graph_status"] == "built"


async def test_existing_table_gets_the_column():
    """历史库里这张表没有这一列。迁移要能就地补上，且历史行是 None——
    它们建没建图无从追溯。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE ingested_documents ("
        "  tenant_id TEXT NOT NULL, file_path TEXT NOT NULL,"
        "  content_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL,"
        "  last_ingested_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (tenant_id, file_path))"
    )
    await conn.execute(
        "INSERT INTO ingested_documents (tenant_id, file_path, content_hash, chunk_count)"
        " VALUES ('t1', 'old.md', 'h', 1)"
    )
    await conn.commit()

    await ensure_tracking_schema(conn)

    rows = await list_tracked_files(conn, tenant_id="t1")
    assert rows[0]["graph_status"] is None
    await conn.close()
