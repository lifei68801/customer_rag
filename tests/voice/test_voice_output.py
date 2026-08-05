from app.providers.tts import TTSRequest, TTSResult
from app.voice.voice_output import (
    synthesize_voice_response,
    synthesize_voice_response_streaming,
)


class FakeTTSProvider:
    def __init__(self) -> None:
        self.requested_texts: list[str] = []

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        self.requested_texts.append(request.text)
        return TTSResult(audio_bytes=f"audio:{request.text}".encode())


async def test_synthesizes_one_segment_per_sentence():
    tts_provider = FakeTTSProvider()

    segments = await synthesize_voice_response(
        "网络断开时请先重启路由器。如果还不行，请检查网线连接！",
        tts_provider=tts_provider,
    )

    assert len(segments) == 2
    assert tts_provider.requested_texts == [
        "网络断开时请先重启路由器。",
        "如果还不行，请检查网线连接！",
    ]


async def test_replaces_unsafe_sentence_with_fallback_phrase_instead_of_dropping():
    tts_provider = FakeTTSProvider()

    await synthesize_voice_response(
        "我的手机号是13812345678，方便联系我。这句是安全的。",
        tts_provider=tts_provider,
    )

    assert tts_provider.requested_texts[0] != "我的手机号是13812345678，方便联系我。"
    assert "无法播报" in tts_provider.requested_texts[0]
    assert tts_provider.requested_texts[1] == "这句是安全的。"


async def _fake_text_stream(deltas: list[str]):
    for delta in deltas:
        yield delta


async def test_streaming_synthesizes_each_sentence_as_soon_as_it_arrives():
    tts_provider = FakeTTSProvider()

    segments = [
        segment
        async for segment in synthesize_voice_response_streaming(
            _fake_text_stream(["网络断开时请先重启路由器。", "如果还不行，请检查网线连接！"]),
            tts_provider=tts_provider,
        )
    ]

    assert len(segments) == 2
    assert tts_provider.requested_texts == [
        "网络断开时请先重启路由器。",
        "如果还不行，请检查网线连接！",
    ]


async def test_streaming_replaces_unsafe_sentence_with_fallback_phrase():
    tts_provider = FakeTTSProvider()

    segments = [
        segment
        async for segment in synthesize_voice_response_streaming(
            _fake_text_stream(["我的手机号是13812345678，方便联系我。", "这句是安全的。"]),
            tts_provider=tts_provider,
        )
    ]

    assert len(segments) == 2
    assert "无法播报" in tts_provider.requested_texts[0]
    assert tts_provider.requested_texts[1] == "这句是安全的。"
