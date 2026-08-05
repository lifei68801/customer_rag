from __future__ import annotations

from typing import AsyncIterator

from app.voice.sentence_segmenter import _TERMINAL_PUNCT, split_sentences


async def stream_sentences(text_stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """消费 LLM 流式生成的文本增量（token/片段流），按句子边界切分并逐句
    yield，而不是等完整回复生成完才切句——这是让"首包延迟"名副其实的
    关键一步（见 docs/ARCHITECTURE.md §7.3），配合 TTS 逐句合成能实现
    真正的"边生成边合成"，不再是"等生成完再统一切句合成"。

    每次收到新的 delta 后重新对累积缓冲区做一次 split_sentences：末尾
    已经带终止标点的部分立刻 yield 出去，缓冲区只保留还没出现终止标点
    的残留文本，供下一个 delta 继续累积；流结束后把缓冲区剩余内容
    （哪怕没有终止标点）作为最后一句 flush 出去。
    """
    buffer = ""
    async for delta in text_stream:
        buffer += delta
        if not buffer.strip():
            continue
        ends_with_terminal_punct = buffer.rstrip()[-1] in _TERMINAL_PUNCT
        sentences = split_sentences(buffer)
        if not sentences:
            continue
        if ends_with_terminal_punct:
            for sentence in sentences:
                yield sentence
            buffer = ""
        else:
            for sentence in sentences[:-1]:
                yield sentence
            buffer = sentences[-1]

    if buffer.strip():
        yield buffer.strip()
