from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_PATTERN = re.compile(r"^## (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    text: str
    heading_path: list[str]
    source: str = ""
    # parent-child 分块：text 是用于 embedding 的细粒度内容（比如表格的
    # 一行），parent_text 是命中后应该返回给 LLM 的完整上下文（比如整张
    # 表）——避免大段表格被切成孤立的行送去 embedding 时，语义被稀释、
    # 精度下降，但命中后又只拿到一行、丢失了表格其余部分的上下文。
    # None 表示 text 本身就是完整上下文，不做 parent-child 区分（默认，
    # 兼容原有的按标题/整页分块）。
    parent_text: str | None = None


def chunk_markdown(text: str, *, source: str) -> list[Chunk]:
    matches = list(_HEADING_PATTERN.finditer(text))
    if not matches:
        stripped = text.strip()
        if not stripped:
            return []
        return [Chunk(text=stripped, heading_path=[], source=source)]

    chunks: list[Chunk] = []
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        chunks.append(
            Chunk(text=body, heading_path=[heading], source=source)
        )
    return chunks
