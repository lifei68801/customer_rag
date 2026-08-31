import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from app.providers.asr import ASRRequest, ASRResult
from app.providers.registry import ProviderRegistry
from tests.settings_factory import build_settings as _settings


async def _override_get_review_conn() -> aiosqlite.Connection:
    # asr_finalize_endpoint 现在直接用 review_conn 查术语表（不再经过已
    # 删除的 deps.get_terms，见 app/api/deps.py 顶部说明）——这两条测试
    # 只关心网关鉴权，跟术语内容无关，空 schema 即可。
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    # Task 3：asr_finalize_endpoint 现在经 list_terms_merged() 读术语表，
    # 测试连接要把 term_edits 表也建好，否则会报 "no such table: term_edits"。
    await ensure_term_edits_schema(conn)
    return conn


class FakeASRProvider:
    async def transcribe(self, request: ASRRequest) -> ASRResult:
        return ASRResult(text="重启路由器")


def test_asr_finalize_rejects_wrong_gateway_secret_when_configured():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_review_conn] = _override_get_review_conn
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/voice/asr/finalize",
            files={"audio": ("test.wav", b"fake-audio-bytes", "audio/wav")},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "wrong"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_asr_finalize_accepts_correct_gateway_secret():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_review_conn] = _override_get_review_conn
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/voice/asr/finalize",
            files={"audio": ("test.wav", b"fake-audio-bytes", "audio/wav")},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "sekret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_asr_stream_closes_with_error_on_wrong_gateway_secret():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        with client.websocket_connect(
            "/voice/asr/stream",
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "wrong"},
        ) as websocket:
            message = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert message["type"] == "error"


def test_asr_stream_accepts_correct_gateway_secret():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        with client.websocket_connect(
            "/voice/asr/stream",
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "sekret"},
        ) as websocket:
            websocket.send_bytes(b"fake-audio-chunk")
            message = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert message["type"] == "partial"
