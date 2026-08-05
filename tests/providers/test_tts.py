import json

import httpx

from app.providers.tts import GenericTTSProvider, TTSRequest


async def test_synthesize_sends_text_and_returns_audio_bytes():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"fake-audio-bytes",
            headers={"content-type": "audio/wav"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GenericTTSProvider(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-key",
        model="cosyvoice-v1",
        client=client,
    )

    result = await provider.synthesize(TTSRequest(text="重启路由器即可解决"))

    assert result.audio_bytes == b"fake-audio-bytes"
    assert captured["body"]["text"] == "重启路由器即可解决"
