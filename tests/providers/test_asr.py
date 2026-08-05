import json

import httpx

from app.providers.asr import ASRRequest, GenericASRProvider


async def test_transcribe_sends_audio_bytes_and_parses_text():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"text": "网络断开时请先重启路由器"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GenericASRProvider(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-key",
        model="paraformer-realtime-v2",
        client=client,
    )

    result = await provider.transcribe(
        ASRRequest(audio_bytes=b"fake-audio-bytes", audio_format="wav")
    )

    assert result.text == "网络断开时请先重启路由器"
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"] == b"fake-audio-bytes"
