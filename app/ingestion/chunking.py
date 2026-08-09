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


def _greedy_merge(pieces: list[str], *, join: str, max_len: int) -> list[str]:
    """把切出来的小片段依次贪心拼接，尽量凑到接近 max_len 但不超过；
    单个片段本身已经超过 max_len 时原样保留（不在这一步再切，交给调用方
    用更细的分隔符继续递归）。
    """
    merged: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{join}{piece}" if current else piece
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            merged.append(current)
        current = piece
    if current:
        merged.append(current)
    return merged


def _merge_and_recurse(fragments: list[str], *, join: str, max_len: int) -> list[str]:
    """贪心合并后对每个片段递归切分。提取的辅助函数，消除 _split_text_recursive
    中段落和句子分支的重复逻辑。"""
    merged = _greedy_merge(fragments, join=join, max_len=max_len)
    result: list[str] = []
    for piece in merged:
        result.extend(_split_text_recursive(piece, max_len=max_len))
    return result


def _split_text_recursive(text: str, *, max_len: int) -> list[str]:
    """递归三级切分：段落（\\n\\n）-> 中文句末标点 -> 硬按字符数截断。
    每一级先贪心合并到接近 max_len，合并后仍超阈值的单个片段再用下一级
    更细的分隔符继续递归；硬切这一级没有更细的分隔符可用，直接截断，
    保证递归一定收敛。
    """
    if len(text) <= max_len:
        return [text]

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) > 1:
        return _merge_and_recurse(paragraphs, join="\n\n", max_len=max_len)

    sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    if len(sentences) > 1:
        return _merge_and_recurse(sentences, join="", max_len=max_len)

    return [text[i : i + max_len] for i in range(0, len(text), max_len)]


def _add_overlap(pieces: list[str], *, overlap: int) -> list[str]:
    """同一个原始 chunk 内部切出的子片段之间加小段重叠，避免硬切边界
    正好切在关键信息中间。第一个子片段不加前缀（它前面没有"上一段"）。
    """
    if overlap <= 0 or len(pieces) <= 1:
        return pieces
    result = [pieces[0]]
    for i in range(1, len(pieces)):
        prev_tail = pieces[i - 1][-overlap:]
        result.append(prev_tail + pieces[i])
    return result


def split_oversized_chunks(
    chunks: list[Chunk], *, max_len: int = 800, overlap: int = 90
) -> list[Chunk]:
    """尺寸兜底：结构感知分块本身没有尺寸上限，某个标题下正文很长、或
    整篇没有任何标题时会产出巨大的 chunk，稀释 embedding 语义。这里对
    超过 max_len 的 chunk 做递归二次切分，只用于 embedding 路径——图谱
    抽取需要更完整的上下文，应该继续吃未经切分的原始 chunk（调用方
    不要把这个函数的输出传给图谱抽取）。

    parent_text 非空的 chunk（PDF 表格行）原样跳过，不做二次切分——那些
    本来就很小，且切分会破坏 parent-child 对应关系。

    不同原始 chunk 之间不重叠、不合并，重叠只发生在"同一个原始 chunk
    内部被迫二次切分"这种情况，否则 heading_path 溯源会失真。

    800/90 这两个默认值是参考起点，不是通过真实数据标定的权威值。
    """
    result: list[Chunk] = []
    for chunk in chunks:
        if chunk.parent_text is not None or len(chunk.text) <= max_len:
            result.append(chunk)
            continue
        pieces = _split_text_recursive(chunk.text, max_len=max_len)
        pieces = _add_overlap(pieces, overlap=overlap)
        for piece in pieces:
            result.append(
                Chunk(text=piece, heading_path=list(chunk.heading_path), source=chunk.source)
            )
    return result
