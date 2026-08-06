import pytest

from app.providers.dashscope_tts import DashScopeTTSProvider
from app.providers.tts import TTSRequest


class FakeSynthesizer:
    def __init__(self, *, audio_bytes: bytes | None, response: dict | None = None) -> None:
        self._audio_bytes = audio_bytes
        self._response = response or {}
        self.last_text: str | None = None

    def call(self, text: str) -> bytes | None:
        self.last_text = text
        return self._audio_bytes

    def get_response(self) -> dict:
        return self._response


async def test_synthesize_returns_audio_bytes_from_the_synthesizer():
    fake = FakeSynthesizer(audio_bytes=b"fake-audio-bytes")
    captured_kwargs: dict = {}

    def factory(*, model: str, voice: str):
        captured_kwargs["model"] = model
        captured_kwargs["voice"] = voice
        return fake

    provider = DashScopeTTSProvider(
        api_key="test-key",
        model="cosyvoice-v3.5-flash",
        voice="my-cloned-voice-id",
        base_websocket_api_url="wss://example.com/api-ws/v1/inference",
        base_http_api_url="https://example.com/api/v1",
        synthesizer_factory=factory,
    )

    result = await provider.synthesize(TTSRequest(text="你好"))

    assert result.audio_bytes == b"fake-audio-bytes"
    assert captured_kwargs == {"model": "cosyvoice-v3.5-flash", "voice": "my-cloned-voice-id"}
    assert fake.last_text == "你好"


async def test_synthesize_raises_with_diagnostic_info_when_call_returns_none():
    # 真实调用中 call() 在合成失败时返回 None（而不是抛异常），错误详情要
    # 靠 get_response() 拿——实测过阿里百炼的这个私有部署端点在音色不
    # 存在时就是这个行为（error_code=InvalidParameter）。
    fake = FakeSynthesizer(
        audio_bytes=None,
        response={"header": {"error_code": "InvalidParameter", "error_message": "boom"}},
    )
    provider = DashScopeTTSProvider(
        api_key="test-key",
        model="cosyvoice-v3.5-flash",
        voice="bad-voice-id",
        base_websocket_api_url="wss://example.com/api-ws/v1/inference",
        base_http_api_url="https://example.com/api/v1",
        synthesizer_factory=lambda *, model, voice: fake,
    )

    with pytest.raises(RuntimeError, match="InvalidParameter"):
        await provider.synthesize(TTSRequest(text="你好"))


async def test_synthesize_configures_dashscope_module_globals_before_calling():
    import dashscope

    fake = FakeSynthesizer(audio_bytes=b"audio")

    def factory(*, model, voice):
        # 工厂被调用时，dashscope 的全局配置应该已经设置好了
        assert dashscope.api_key == "test-key"
        assert dashscope.base_websocket_api_url == "wss://example.com/api-ws/v1/inference"
        assert dashscope.base_http_api_url == "https://example.com/api/v1"
        return fake

    provider = DashScopeTTSProvider(
        api_key="test-key",
        model="cosyvoice-v3.5-flash",
        voice="voice-id",
        base_websocket_api_url="wss://example.com/api-ws/v1/inference",
        base_http_api_url="https://example.com/api/v1",
        synthesizer_factory=factory,
    )

    await provider.synthesize(TTSRequest(text="你好"))
