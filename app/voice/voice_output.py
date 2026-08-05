from __future__ import annotations

from app.providers.tts import TTSProvider, TTSRequest
from app.safety.rules import check_text
from app.voice.sentence_segmenter import split_sentences

_UNSAFE_SENTENCE_FALLBACK = "本句内容无法播报。"


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
    """
    sentences = split_sentences(text)
    audio_segments: list[bytes] = []
    for sentence in sentences:
        safety = check_text(sentence, banned_terms=banned_terms)
        speak_text = sentence if safety.is_safe else _UNSAFE_SENTENCE_FALLBACK
        result = await tts_provider.synthesize(TTSRequest(text=speak_text))
        audio_segments.append(result.audio_bytes)
    return audio_segments
