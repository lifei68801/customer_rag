from __future__ import annotations

import re

_TERMINAL_PUNCT = "。！？!?"
_SPLIT_PATTERN = re.compile(rf"(?<=[{_TERMINAL_PUNCT}])")


def split_sentences(text: str) -> list[str]:
    """按句末标点切分句子，驱动句子级流式 TTS 的触发时机。

    末尾没有终止标点的残留文本也作为最后一句返回（对应流式场景里
    "LLM 还没生成完，最后一段暂时没有标点"的情况）。
    """
    stripped = text.strip()
    if not stripped:
        return []
    parts = [p.strip() for p in _SPLIT_PATTERN.split(stripped) if p.strip()]
    return parts
