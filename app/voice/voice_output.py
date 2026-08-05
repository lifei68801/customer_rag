from __future__ import annotations

from typing import AsyncIterator

from app.providers.tts import TTSProvider, TTSRequest
from app.safety.rules import check_text
from app.voice.sentence_segmenter import split_sentences
from app.voice.streaming_responder import stream_sentences

_UNSAFE_SENTENCE_FALLBACK = "本句内容无法播报。"


async def _synthesize_one_sentence(
    sentence: str,
    *,
    tts_provider: TTSProvider,
    banned_terms: list[str] | None,
) -> bytes:
    safety = check_text(sentence, banned_terms=banned_terms)
    speak_text = sentence if safety.is_safe else _UNSAFE_SENTENCE_FALLBACK
    result = await tts_provider.synthesize(TTSRequest(text=speak_text))
    return result.audio_bytes


async def synthesize_voice_response(
    text: str,
    *,
    tts_provider: TTSProvider,
    banned_terms: list[str] | None = None,
) -> list[bytes]:
    """句子级合成：每句先过轻量规则安全检查，命中风险词则替换为兜底话术再合成。

    "替换后仍合成"而非"直接丢弃"——保证语音回复听起来是完整的一段话，
    不会因为中间某句被静默跳过而显得莫名其妙断掉。这是分句轻量检查，
    完整语义级安全审查仍由第9节输出安全层负责，二者不冲突。

    这是"批量"版本：入参是已经生成完的完整文本。首包延迟意义上真正
    有用的是 synthesize_voice_response_streaming()——边生成边合成。
    """
    sentences = split_sentences(text)
    return [
        await _synthesize_one_sentence(
            sentence, tts_provider=tts_provider, banned_terms=banned_terms
        )
        for sentence in sentences
    ]


async def synthesize_voice_response_streaming(
    text_stream: AsyncIterator[str],
    *,
    tts_provider: TTSProvider,
    banned_terms: list[str] | None = None,
) -> AsyncIterator[bytes]:
    """流式版本：消费 LLM 的流式文本增量（如
    `OpenAICompatibleChatProvider.stream_complete()` 的输出），句子一
    出现终止标点就立刻合成并 yield 音频，不等整段回复生成完——这是
    docs/ARCHITECTURE.md §7.3"首包延迟=等第一句生成完+该句合成耗时"
    而不是"等完整回复生成完"的实现方式。

    调用方目前只有测试；真正接入 /agent/chat 的 SSE 响应需要 Responder
    节点本身先改成用 stream_complete() 而不是 complete()，且 LangGraph
    的节点级 dict 更新模型不直接支持"节点内部流式产出多个增量结果"，
    这是一个更大的、独立的图结构调整，本次不做——这里只把"流式生成到
    流式合成"这条能力链路建好、验证过，接入 SSE 端点是后续工作。
    """
    async for sentence in stream_sentences(text_stream):
        yield await _synthesize_one_sentence(
            sentence, tts_provider=tts_provider, banned_terms=banned_terms
        )
