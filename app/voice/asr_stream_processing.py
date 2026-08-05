from __future__ import annotations

# 只处理独立语气词/口头禅性质的填充词，不做语法感知过滤——"这个""那个""啊"
# 这类词可能是句子成分或语气助词，误删会改变语义，刻意不放进这个列表。
_FILLER_WORDS = ("嗯", "呃", "额", "唉", "哦", "噢")


def merge_chunk_transcript(committed: str, new_chunk: str, *, min_overlap: int = 2) -> str:
    """合并流式ASR新分片转写文本到已确认文本，去除分片边界重叠部分。

    分片音频常带重叠窗口以提升边界处识别质量，导致相邻分片的转写文本
    首尾重复；这里只处理"committed 的尾部恰好是 new_chunk 的前缀"这种
    精确重叠，找不到就直接拼接（不做模糊匹配，避免把本来不重复的内容
    误判为重叠而丢字）。
    """
    if not committed:
        return new_chunk
    if not new_chunk:
        return committed

    max_overlap = min(len(committed), len(new_chunk))
    for overlap_len in range(max_overlap, min_overlap - 1, -1):
        if committed[-overlap_len:] == new_chunk[:overlap_len]:
            return committed + new_chunk[overlap_len:]
    return committed + new_chunk


def filter_filler_words(text: str) -> str:
    """去除文本中的独立语气词/填充词（嗯、呃、额、唉、哦、噢）。"""
    result = text
    for filler in _FILLER_WORDS:
        result = result.replace(filler, "")
    return result
