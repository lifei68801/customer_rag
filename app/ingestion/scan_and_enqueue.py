from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.ingestion.ingestion_queue import enqueue_ingestion_job
from app.ingestion.scan_changes import scan_for_changes
from app.ingestion.tracking import compute_file_hash


async def scan_and_enqueue(
    directory: Path,
    *,
    tenant_id: str,
    conn: aiosqlite.Connection,
) -> dict[str, int]:
    """扫描目录，把新增/变更/已删除的文件各自入队，未变的跳过。

    conn 需要同时具备 ingested_documents 和 ingestion_jobs 两张表
    （ensure_tracking_schema + ensure_ingestion_queue_schema），因为
    scan_for_changes 读 tracking 表，enqueue_ingestion_job 写队列表，
    调用方通常是同一个 SQLite 连接。

    返回各类文件的数量统计，供调用方打印摘要；这一步本身只入队，不做
    真正的解析/向量化/写入——那部分交给 ingestion_queue.process_pending_jobs()。
    """
    change_set = await scan_for_changes(directory, tenant_id=tenant_id, tracking_conn=conn)

    for file_path in change_set.new_files + change_set.changed_files:
        await enqueue_ingestion_job(
            conn,
            tenant_id=tenant_id,
            file_path=str(file_path),
            content_hash=compute_file_hash(file_path),
            action="ingest",
        )

    for deleted_path in change_set.deleted_file_paths:
        await enqueue_ingestion_job(
            conn,
            tenant_id=tenant_id,
            file_path=deleted_path,
            content_hash="",
            action="delete",
        )

    return {
        "new": len(change_set.new_files),
        "changed": len(change_set.changed_files),
        "deleted": len(change_set.deleted_file_paths),
        "unchanged": len(change_set.unchanged_files),
    }
