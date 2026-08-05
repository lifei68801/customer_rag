from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.providers.asr import ASRRequest, ASRResult


class FakeASRProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def transcribe(self, request: ASRRequest) -> ASRResult:
        return ASRResult(text=self._responses.pop(0) if self._responses else "")


def test_asr_stream_returns_partial_then_final_text():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider(
        ["网络", "断开了"]
    )
    try:
        client = TestClient(app)
        with client.websocket_connect("/voice/asr/stream") as ws:
            ws.send_bytes(b"chunk-1")
            first = ws.receive_json()
            ws.send_bytes(b"chunk-2")
            second = ws.receive_json()
            ws.send_text("stop")
            final = ws.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert first == {"type": "partial", "text": "网络"}
    assert second == {"type": "partial", "text": "断开了"}
    assert final == {"type": "final", "text": "网络断开了"}


def test_asr_stream_merges_overlapping_chunk_boundary():
    # 分片音频重叠窗口导致相邻分片转写文本首尾重复，不应在最终结果里重复出现
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider(
        ["我们讨论一下这个方案", "这个方案有三个优点"]
    )
    try:
        client = TestClient(app)
        with client.websocket_connect("/voice/asr/stream") as ws:
            ws.send_bytes(b"chunk-1")
            ws.receive_json()
            ws.send_bytes(b"chunk-2")
            second = ws.receive_json()
            ws.send_text("stop")
            final = ws.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert second == {"type": "partial", "text": "有三个优点"}
    assert final == {"type": "final", "text": "我们讨论一下这个方案有三个优点"}


def test_asr_stream_filters_filler_words_in_final_text():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider(
        ["嗯我们讨论一下呃这个方案"]
    )
    try:
        client = TestClient(app)
        with client.websocket_connect("/voice/asr/stream") as ws:
            ws.send_bytes(b"chunk-1")
            ws.receive_json()
            ws.send_text("stop")
            final = ws.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert final == {"type": "final", "text": "我们讨论一下这个方案"}
