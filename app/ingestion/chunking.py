from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_PATTERN = re.compile(r"^## (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    text: str
    heading_path: list[str]
    source: str = ""


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
