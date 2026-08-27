from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.providers.asr import ASRRequest, ASRResult
from tests.settings_factory import build_settings


_settings = build_settings


class FakeASRProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def transcribe(self, request: ASRRequest) -> ASRResult:
        return ASRResult(text=self._responses.pop(0) if self._responses else "")


def test_asr_stream_returns_partial_then_final_text():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider(
        ["网络", "断开了"]
    )
    # gateway_shared_secret 显式钉死为 None：不 override 的话 get_settings
    # 会读真实环境变量/.env，一旦开发者本机或 .env 配置了
    # CUSTOMER_RAG_GATEWAY_SHARED_SECRET（正是这个安全修复要促使运营者去做
    # 的事），这条与租户鉴权无关的测试会意外因缺少网关凭证被拒绝而失败。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        # gateway_shared_secret 未配置，resolve_tenant_id() 走 fallback
        # 降级路径，这里显式带上 tenant_id query 参数，避免因缺少任何租户
        # 身份而被关闭连接——这些测试关注的是流式转写/去重合并/语气词过滤
        # 逻辑，与租户鉴权无关。
        with client.websocket_connect("/voice/asr/stream?tenant_id=t1") as ws:
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
    # gateway_shared_secret 显式钉死为 None，理由同上一条测试。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        # gateway_shared_secret 未配置，resolve_tenant_id() 走 fallback
        # 降级路径，这里显式带上 tenant_id query 参数，避免因缺少任何租户
        # 身份而被关闭连接——这些测试关注的是流式转写/去重合并/语气词过滤
        # 逻辑，与租户鉴权无关。
        with client.websocket_connect("/voice/asr/stream?tenant_id=t1") as ws:
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
    # gateway_shared_secret 显式钉死为 None，理由同上一条测试。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        # gateway_shared_secret 未配置，resolve_tenant_id() 走 fallback
        # 降级路径，这里显式带上 tenant_id query 参数，避免因缺少任何租户
        # 身份而被关闭连接——这些测试关注的是流式转写/去重合并/语气词过滤
        # 逻辑，与租户鉴权无关。
        with client.websocket_connect("/voice/asr/stream?tenant_id=t1") as ws:
            ws.send_bytes(b"chunk-1")
            ws.receive_json()
            ws.send_text("stop")
            final = ws.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert final == {"type": "final", "text": "我们讨论一下这个方案"}
