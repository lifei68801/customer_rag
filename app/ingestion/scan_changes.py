from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.ingestion.tracking import compute_file_hash, list_tracked_files

_SUPPORTED_PATTERNS = ("*.md", "*.pdf", "*.docx", "*.png", "*.jpg", "*.jpeg", "*.csv")


@dataclass(frozen=True)
class ChangeSet:
    new_files: list[Path]
    changed_files: list[Path]
    unchanged_files: list[Path]
    deleted_file_paths: list[str]


async def scan_for_changes(
    directory: Path,
    *,
    tenant_id: str,
    tracking_conn: aiosqlite.Connection,
) -> ChangeSet:
    """对比目录当前状态和 ingested_documents 里记录的上次摄取状态，算出
    新增/变更/未变/已删除四类文件，供调用方只把新增+变更的入队处理——
    这是"只处理变化部分"的核心逻辑，本身不做任何写入（既不碰摄取，也
    不碰 tracking 表），纯粹是一次对比。
    """
    current_files = sorted(
        {f for pattern in _SUPPORTED_PATTERNS for f in directory.glob(pattern)}
    )
    tracked = await list_tracked_files(tracking_conn, tenant_id=tenant_id)
    tracked_by_path = {t["file_path"]: t["content_hash"] for t in tracked}

    new_files: list[Path] = []
    changed_files: list[Path] = []
    unchanged_files: list[Path] = []
    seen_paths: set[str] = set()

    for file_path in current_files:
        path_str = str(file_path)
        seen_paths.add(path_str)
        current_hash = compute_file_hash(file_path)
        tracked_hash = tracked_by_path.get(path_str)
        if tracked_hash is None:
            new_files.append(file_path)
        elif tracked_hash != current_hash:
            changed_files.append(file_path)
        else:
            unchanged_files.append(file_path)

    deleted_file_paths = [
        path_str for path_str in tracked_by_path if path_str not in seen_paths
    ]

    return ChangeSet(
        new_files=new_files,
        changed_files=changed_files,
        unchanged_files=unchanged_files,
        deleted_file_paths=deleted_file_paths,
    )
