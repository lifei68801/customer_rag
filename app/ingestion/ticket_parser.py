from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.chunking import Chunk


@dataclass(frozen=True)
class TicketColumnMapping:
    """把 CSV 表头映射到语义字段——不同工单系统导出的列名差异很大
    （"subject"/"标题"/"问题标题"...），不假设固定列名，调用方按实际
    导出文件传入自定义映射。"""

    ticket_id: str = "ticket_id"
    subject: str = "subject"
    description: str = "description"
    resolution: str = "resolution"


def parse_ticket_csv(
    path: Path,
    *,
    column_mapping: TicketColumnMapping | None = None,
    resolved_only: bool = True,
    encoding: str = "utf-8-sig",
) -> list[Chunk]:
    """把历史工单 CSV 导出文件解析成 chunk，每条工单（问题+处理方案）
    作为一个独立 chunk——不做进一步拆分，一条工单本身就是一个完整、
    自洽的问答单元，不像表格那样需要 parent-child 两级分块。

    resolved_only=True（默认）：跳过没有处理方案的工单——一条"还没解决"
    的工单不构成可复用的知识，只有已解决的历史工单才适合作为知识库内容。

    encoding 默认 utf-8-sig（容忍 Excel 导出常见的 UTF-8 BOM）；如果
    导出文件是 GBK 等其他编码，调用方需要自行传入正确的 encoding 或
    先转码，这里不做编码自动探测（不可靠，容易猜错）。
    """
    mapping = column_mapping or TicketColumnMapping()
    chunks: list[Chunk] = []

    with open(path, encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            resolution = (row.get(mapping.resolution) or "").strip()
            if resolved_only and not resolution:
                continue

            subject = (row.get(mapping.subject) or "").strip()
            description = (row.get(mapping.description) or "").strip()
            ticket_id = (row.get(mapping.ticket_id) or "").strip()

            parts: list[str] = []
            if subject:
                parts.append(f"问题：{subject}")
            if description and description != subject:
                parts.append(f"详情：{description}")
            if resolution:
                parts.append(f"解决方案：{resolution}")
            text = "\n".join(parts)
            if not text:
                continue

            heading = [f"工单{ticket_id}"] if ticket_id else ["历史工单"]
            chunks.append(Chunk(text=text, heading_path=heading, source=str(path)))

    return chunks
